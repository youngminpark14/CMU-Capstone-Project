"""The dry-run estimate: its maximum bounds what an episode can spend, repair retries aside."""

from __future__ import annotations

from decimal import Decimal

from pmfa.config import Config
from pmfa.evaluation.estimate import CallPricer, _episode_bound
from pmfa.records.roles import ModelSlot, RoleTask


def _worst_case(depth: int, config: Config, price: CallPricer) -> Decimal:
    """Every call an episode of ``depth`` can make, with each re-score on the slot of its level.

    The engine re-scores a parent's children once after its evidence follow-ups,
    on the critic slot of that level: the depth-1 critic at the first level, the
    deep critic below it.
    """
    parents = 1 + (depth - 1) * (config.forecast.beam_width + 1)
    followups = config.forecast.max_evidence_followups
    scores = [price(RoleTask.SCORE, ModelSlot.CRITIC_DEPTH1)] + [
        price(RoleTask.SCORE, ModelSlot.CRITIC_DEEP)
    ] * (parents - 1)
    calls = [
        price(RoleTask.PLAN_QUERIES, ModelSlot.RESEARCHER),
        price(RoleTask.SELECT_PASSAGES, ModelSlot.RESEARCHER),
        *[price(RoleTask.GENERATE, ModelSlot.GENERATOR)] * parents,
        *scores,
        *[price(RoleTask.SELECT_PASSAGES, ModelSlot.RESEARCHER)] * followups,
        *sorted(scores, reverse=True)[: min(followups, parents)],
    ]
    calls = sorted(calls, reverse=True)[: config.budget.max_calls_per_episode]
    return sum(calls, Decimal("0"))


def test_the_maximum_prices_a_first_level_re_score_on_the_depth_1_critic() -> None:
    config = Config()
    price = CallPricer(config, {})
    deep = price(RoleTask.SCORE, ModelSlot.CRITIC_DEEP)
    first = price(RoleTask.SCORE, ModelSlot.CRITIC_DEPTH1)
    assert first > deep  # otherwise this test would not tell the two apart
    for depth in (1, 2, 3):
        calls, usd = _episode_bound(depth, config, price, match=False)
        assert usd >= _worst_case(depth, config, price), depth
    # A depth-1 tree has one scoring call, so at most one re-score, on the depth-1 critic.
    calls, usd = _episode_bound(1, config, price, match=False)
    assert usd == _worst_case(1, config, price)
    assert calls == 2 + 1 + 1 + config.forecast.max_evidence_followups + 1


def test_the_plan_warns_when_its_maximum_exceeds_the_cap(
    data_config: Config, tmp_path, capsys
) -> None:  # type: ignore[no-untyped-def]
    from pmfa.cli.backtest import _print_plan
    from pmfa.evaluation.backtest import run_backtest

    config = data_config.model_copy(
        update={
            "budget": data_config.budget.model_copy(update={"max_usd_per_run": Decimal("0.01")})
        }
    )
    manifest = run_backtest(
        config, run_dir=tmp_path / "dry", systems=["market", "pipeline"], dry_run=True
    )
    assert manifest.plan.usd_max > Decimal("0.01")
    _print_plan(manifest)
    out = capsys.readouterr().out
    assert "exceeds the run's dollar cap" in out and "partial" in out


def test_baseline_calls_are_not_priced_from_a_blend_of_recorded_answers() -> None:
    config = Config()
    model = config.models.slots[ModelSlot.BASELINE].model
    price = CallPricer(config, {("baseline", model): (Decimal("9"), 4)})
    assert price(RoleTask.BASELINE, ModelSlot.BASELINE, with_passages=False) < price(
        RoleTask.BASELINE, ModelSlot.BASELINE, with_passages=True
    )
    assert Decimal("9") not in {
        price(RoleTask.BASELINE, ModelSlot.BASELINE, with_passages=flag) for flag in (True, False)
    }


def test_the_pipeline_plan_says_its_maximum_counts_no_repair_retries(
    data_config: Config, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Each unusable answer gets one metered retry, which the maximum does not count: the
    plan says so next to it, and names the run's dollar cap as the real ceiling."""
    from pmfa.evaluation.backtest import run_backtest

    manifest = run_backtest(
        data_config, run_dir=tmp_path / "dry", systems=["market", "pipeline"], dry_run=True
    )
    [pipeline] = [p for p in manifest.plan.systems if p.system == "pipeline"]
    [note] = [n for n in pipeline.notes if "repair retr" in n]
    assert "no repair retries" in note and "dollar cap" in note
    assert f"${data_config.budget.max_usd_per_run}" in note
