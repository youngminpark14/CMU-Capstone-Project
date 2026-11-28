import json
from decimal import Decimal

import pytest

from pmfa.errors import BudgetExhausted, CassetteMiss, ModelOutputError, RunBudgetExhausted
from pmfa.models import CassettePlayer, CassetteRecorder, Gateway
from pmfa.models.roles.runner import DirectRunner
from pmfa.ports import Models
from pmfa.records.roles import MatchDecision, ModelSlot, RoleTask, ScoreOutput

from .fakes import FakeBackend, ok, raw
from .fixtures import AS_OF, INPUTS, OUTPUTS, make_config

MATCH_IN = INPUTS[RoleTask.MATCH]
MATCH_OUT = OUTPUTS[RoleTask.MATCH]


class Ticker:
    """A fake monotonic timer: each call advances by ``step`` seconds."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def gateway(answers, *, config=None, **kwargs):
    backend = FakeBackend(list(answers))
    config = config or make_config()
    return Gateway(config, runner=DirectRunner(backend), **kwargs), backend


def test_a_valid_answer_comes_back_with_its_cost_and_slot():
    gw, backend = gateway([ok(MATCH_OUT, tokens=(10_000, 1_000))])
    assert isinstance(gw, Models)
    result = gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert result.output == MATCH_OUT
    meta = result.meta
    assert (meta.slot, meta.model, meta.effort, meta.source) == (
        ModelSlot.MONITOR,
        "claude-haiku-5-5",
        "low",
        "live",
    )
    assert meta.cost_usd == Decimal("0.0015")  # 10k x $0.10/M + 1k x $0.50/M
    assert not meta.repaired
    assert backend.requests[0]["max_tokens"] == 4000
    assert gw.usage("e").calls == 1 and gw.usage("e").cost_usd == Decimal("0.0015")


def test_a_named_slot_overrides_the_tasks_default():
    gw, backend = gateway([ok(OUTPUTS[RoleTask.SCORE])])
    result = gw.call(
        RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e", slot=ModelSlot.CRITIC_DEPTH1
    )
    assert result.meta.model == "claude-fable-5-2"
    assert backend.requests[0]["model"] == "claude-fable-5-2"
    assert backend.requests[0]["effort"] == "high"


def test_the_wrong_input_record_is_refused():
    gw, backend = gateway([])
    with pytest.raises(TypeError):
        gw.call(RoleTask.MATCH, INPUTS[RoleTask.PLAN_QUERIES], episode_id="e")
    assert backend.requests == []


def test_malformed_output_gets_one_repair_retry_showing_the_bad_answer():
    bad = '{"matched": "maybe", "event_ticker": "", "rationale": "x"}'
    gw, backend = gateway([raw(bad), ok(MATCH_OUT)])
    result = gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert result.output == MATCH_OUT and result.meta.repaired
    assert len(backend.requests) == 2 and gw.usage("e").calls == 2
    repair_text = backend.requests[1]["user"]
    assert bad in repair_text and "matched" in repair_text


def test_malformed_twice_is_a_model_output_error():
    gw, backend = gateway([raw("not json"), raw("still not json")])
    with pytest.raises(ModelOutputError):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 2


def test_a_failing_validator_counts_as_malformed():
    # Quantiles must be non-decreasing; the schema cannot say so, the validator does.
    bad = OUTPUTS[RoleTask.BASELINE].model_dump(mode="json")
    bad["quantiles"] = {"q05": 3, "q25": 2, "q50": 1, "q75": 0, "q95": -1}
    gw, _ = gateway([raw(json.dumps(bad)), raw(json.dumps(bad))])
    with pytest.raises(ModelOutputError, match="non-decreasing"):
        gw.call(RoleTask.BASELINE, INPUTS[RoleTask.BASELINE], episode_id="e")


def test_a_refusal_is_retried_once_then_a_model_output_error():
    gw, backend = gateway([raw("", stop_reason="refusal"), raw("", stop_reason="refusal")])
    with pytest.raises(ModelOutputError, match="declined"):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 2


def test_a_truncated_answer_is_not_accepted_even_if_it_parses():
    truncated = raw(MATCH_OUT.model_dump_json(), stop_reason="max_tokens")
    gw, _ = gateway([truncated, truncated])
    with pytest.raises(ModelOutputError, match="max_tokens"):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")


def test_the_call_cap_stops_before_calling():
    config = make_config(budget={"max_calls_per_episode": 4})
    gw, backend = gateway([ok(MATCH_OUT)] * 5, config=config)
    for _ in range(4):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(BudgetExhausted):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 4
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="another")


def test_a_repair_retry_also_needs_budget():
    config = make_config(budget={"max_calls_per_episode": 4})
    gw, backend = gateway([ok(MATCH_OUT)] * 3 + [raw("bad")] * 2, config=config)
    for _ in range(3):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(BudgetExhausted):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 4


SCORE_IN = INPUTS[RoleTask.SCORE]


def score_worst_case(config) -> Decimal:
    """The most one SCORE call can cost: its input-token bound plus max_tokens of output."""
    from pmfa.models.gateway import input_token_bound
    from pmfa.models.pricing import cost
    from pmfa.models.roles.prompts import render
    from pmfa.records.roles import TASKS

    assignment = config.models.slots[TASKS[RoleTask.SCORE].default_slot]
    tokens = input_token_bound(render(RoleTask.SCORE, SCORE_IN), TASKS[RoleTask.SCORE].output_type)
    return cost(assignment.model, tokens, assignment.max_tokens, catalog=config.models.catalog)


def test_the_episode_dollar_cap_is_a_ceiling():
    config = make_config(budget={"max_usd_per_episode": "0.30", "max_usd_per_run": "25"})
    worst = score_worst_case(config)
    assert Decimal("0.10") < worst < Decimal("0.20")  # 16,000 output tokens alone are $0.16
    # Each Sonnet call: 40k in x $2/M + 2k out x $10/M = $0.10.
    answer = ok(OUTPUTS[RoleTask.SCORE], tokens=(40_000, 2_000))
    gw, backend = gateway([answer] * 3, config=config)
    gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")  # $0.10 spent: $0.10 + worst fits
    # $0.20 spent, $0.10 left: the third call could cost more than that, so it is not sent.
    with pytest.raises(BudgetExhausted, match="dollar"):
        gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert len(backend.requests) == 2
    assert gw.usage("e").cost_usd == Decimal("0.20") <= config.budget.max_usd_per_episode


@pytest.mark.parametrize(
    ("spent", "error"),
    [("episode", BudgetExhausted), ("run", RunBudgetExhausted)],
)
def test_one_cent_below_a_dollar_cap_no_call_is_sent(spent, error):
    """At $1.99 of $2.00 (or $24.99 of $25.00) the next call could cost more than the cent
    left: it is refused, so the cap holds instead of ending near a call's cost above it."""
    from pmfa.records.episode import Usage

    config = make_config(budget={"max_usd_per_episode": "2.00", "max_usd_per_run": "25.00"})
    gw, backend = gateway([ok(OUTPUTS[RoleTask.SCORE], tokens=(400_000, 20_000))], config=config)
    if spent == "episode":
        gw.meter.add("e", Usage(calls=1, cost_usd=Decimal("1.99")))
    else:
        gw.meter.add("old", Usage(calls=1, cost_usd=Decimal("24.99")))
    with pytest.raises(error, match="could cost up to"):
        gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert backend.requests == []
    assert gw.run_usage().cost_usd <= config.budget.max_usd_per_run


def test_a_repair_retry_also_needs_dollar_budget_for_its_worst_case():
    worst = score_worst_case(make_config())
    # Room for the first call's worst case, but not for a second one after it is paid.
    cap = str(worst + Decimal("0.05"))
    config = make_config(budget={"max_usd_per_episode": cap, "max_usd_per_run": "25"})
    gw, backend = gateway([raw("bad", tokens=(40_000, 2_000)), raw("bad")], config=config)
    with pytest.raises(BudgetExhausted, match="dollar"):
        gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert len(backend.requests) == 1


def test_a_cached_answer_is_served_whatever_its_worst_case(tmp_path):
    """A cached answer costs the run nothing new, so only what was spent can refuse it."""
    from pmfa.records.episode import Usage

    answer = ok(OUTPUTS[RoleTask.SCORE], tokens=(40_000, 2_000))
    first, _ = gateway([answer], cache_dir=tmp_path)
    first.call(RoleTask.SCORE, SCORE_IN, episode_id="e")

    config = make_config(budget={"max_usd_per_episode": "2.00", "max_usd_per_run": "25.00"})
    second, backend = gateway([], config=config, cache_dir=tmp_path)
    second.meter.add("old", Usage(calls=1, cost_usd=Decimal("24.99")))
    cached = second.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert cached.meta.source == "cache" and backend.requests == []


def test_the_seconds_cap_applies_only_when_enforced():
    config = make_config(budget={"max_seconds_per_episode": 5})
    answers = [ok(MATCH_OUT)] * 3
    lax, _ = gateway(answers, config=config, timer=Ticker(10.0))
    lax.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    lax.call(RoleTask.MATCH, MATCH_IN, episode_id="e")  # 10 s spent, not enforced

    strict, backend = gateway(answers, config=config, timer=Ticker(10.0), enforce_seconds=True)
    strict.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert strict.usage("e").seconds == pytest.approx(10.0)
    with pytest.raises(BudgetExhausted, match="seconds"):
        strict.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 1


def test_an_enforced_seconds_cap_bounds_each_calls_timeout_by_the_time_left():
    config = make_config(
        budget={"max_seconds_per_episode": 90}, models={"request_timeout_seconds": 180}
    )
    answers = [ok(MATCH_OUT)] * 3
    strict, backend = gateway(answers, config=config, timer=Ticker(30.0), enforce_seconds=True)
    strict.call(RoleTask.MATCH, MATCH_IN, episode_id="e")  # 30 s spent
    strict.call(RoleTask.MATCH, MATCH_IN, episode_id="e")  # 60 s spent
    assert [r["timeout"] for r in backend.requests] == [pytest.approx(90.0), pytest.approx(60.0)]
    strict.call(RoleTask.MATCH, MATCH_IN, episode_id="other")
    assert backend.requests[-1]["timeout"] == pytest.approx(90.0)  # a fresh episode

    lax, backend = gateway([ok(MATCH_OUT)] * 2, config=config, timer=Ticker(30.0))
    lax.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    lax.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert [r["timeout"] for r in backend.requests] == [180.0, 180.0]  # not enforced


def test_a_short_seconds_cap_never_lengthens_the_request_timeout():
    config = make_config(
        budget={"max_seconds_per_episode": 600}, models={"request_timeout_seconds": 180}
    )
    gw, backend = gateway([ok(MATCH_OUT)], config=config, enforce_seconds=True)
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert backend.requests[0]["timeout"] == 180.0


def test_the_run_dollar_cap_raises_run_budget_exhausted():
    config = make_config(budget={"max_usd_per_episode": "0.30", "max_usd_per_run": "0.30"})
    answer = ok(OUTPUTS[RoleTask.SCORE], tokens=(40_000, 2_000))  # $0.10
    gw, backend = gateway([answer] * 3, config=config)
    gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e1")
    gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e2")
    with pytest.raises(RunBudgetExhausted):  # $0.20 spent; the worst case would pass $0.30
        gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e3")
    assert len(backend.requests) == 2
    assert gw.run_usage().cost_usd == Decimal("0.20")


def test_cache_hit_skips_the_model_and_keeps_the_recorded_cost(tmp_path):
    first, _ = gateway([ok(MATCH_OUT, tokens=(10_000, 1_000))], cache_dir=tmp_path)
    original = first.call(RoleTask.MATCH, MATCH_IN, episode_id="e")

    second, backend2 = gateway([], cache_dir=tmp_path)
    cached = second.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert backend2.requests == []
    assert cached.output == original.output
    assert cached.meta.source == "cache"
    assert cached.meta.cost_usd == original.meta.cost_usd
    assert cached.meta.latency_ms == original.meta.latency_ms
    assert second.usage("e").calls == 1  # a cached answer counts as a call
    assert second.run_usage().cost_usd == Decimal("0")  # but the run pays nothing new
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_as_of_is_part_of_the_cache_key(tmp_path):
    later = MATCH_IN.model_copy(update={"as_of": AS_OF.replace(hour=16)})
    gw, backend = gateway([ok(MATCH_OUT), ok(MATCH_OUT)], cache_dir=tmp_path)
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    gw.call(RoleTask.MATCH, later, episode_id="e")
    assert len(backend.requests) == 2
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 2


def test_failed_answers_are_not_cached(tmp_path):
    gw, backend = gateway([raw("x"), raw("y"), ok(MATCH_OUT)], cache_dir=tmp_path)
    with pytest.raises(ModelOutputError):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 3


def test_cassette_records_then_replays(tmp_path):
    no_match = MatchDecision(matched=False, event_ticker="", rationale="No.")
    recorder = CassetteRecorder(tmp_path)
    config = make_config(budget={"max_calls_per_episode": 4})
    gw, _ = gateway(
        [ok(MATCH_OUT), raw("bad"), ok(no_match), raw("x"), raw("y"), raw("z")],
        config=config,
        recorder=recorder,
    )
    live = [gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1").output]
    live.append(gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1").output)  # repaired
    with pytest.raises(BudgetExhausted):  # 3 calls used; a 4th fits, its repair does not
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1")
    with pytest.raises(ModelOutputError):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-2")

    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert files == ["ep-1.json", "ep-2.json"]

    player = CassettePlayer.from_paths(tmp_path)
    assert isinstance(player, Models)
    replayed = [player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1").output for _ in range(2)]
    assert replayed == live
    with pytest.raises(BudgetExhausted):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1")
    with pytest.raises(ModelOutputError):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-2")
    assert player.usage("ep-1").calls == gw.usage("ep-1").calls == 4
    assert player.usage("ep-1").cost_usd == gw.usage("ep-1").cost_usd
    assert player.remaining("ep-1") == 0


def test_a_replay_charges_the_same_seconds_as_the_live_calls_did(tmp_path):
    """A live call does not take a whole number of milliseconds; a cassette stores milliseconds."""
    gw, _ = gateway(
        [ok(MATCH_OUT), raw("bad"), ok(MATCH_OUT), ok(MATCH_OUT), raw("x"), raw("y")],
        recorder=CassetteRecorder(tmp_path),
        timer=Ticker(1.23456789),  # every attempt takes this long
    )
    for _ in range(3):  # the second call is repaired: two attempts
        assert gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1").output == MATCH_OUT
    with pytest.raises(ModelOutputError):  # two attempts, no usable answer
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-2")

    player = CassettePlayer.from_paths(tmp_path)
    for _ in range(3):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1")
    with pytest.raises(ModelOutputError):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-2")
    assert player.usage("ep-1") == gw.usage("ep-1")
    assert player.usage("ep-2") == gw.usage("ep-2")
    assert gw.usage("ep-1").seconds == 4.94  # four attempts of 1.235 s
    assert gw.usage("ep-2").seconds == 2.47

    # Uneven attempts: 0.3 s, then a repaired call of 0.1 s and 0.2 s. Their float sum is
    # not 0.6 unless the meter keeps seconds to the millisecond.
    ends = iter([0.0, 0.3, 0.3, 0.4, 0.4, 0.6])
    uneven, _ = gateway(
        [ok(MATCH_OUT), raw("bad"), ok(MATCH_OUT)],
        recorder=CassetteRecorder(tmp_path / "uneven"),
        timer=lambda: next(ends),
    )
    for _ in range(2):
        uneven.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-3")
    player = CassettePlayer.from_paths(tmp_path / "uneven")
    for _ in range(2):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-3")
    assert player.usage("ep-3") == uneven.usage("ep-3")


def test_replay_with_a_changed_input_is_a_cassette_miss(tmp_path):
    gw, _ = gateway([ok(MATCH_OUT)], recorder=CassetteRecorder(tmp_path))
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    changed = MATCH_IN.model_copy(
        update={"candidates": [MATCH_IN.candidates[0].model_copy(update={"title": "Other"})]}
    )
    player = CassettePlayer.from_paths(tmp_path)
    with pytest.raises(CassetteMiss, match=r"candidates\[0\]\.title.*Other"):
        player.call(RoleTask.MATCH, changed, episode_id="ep")


def test_replay_of_a_different_task_or_an_extra_call_is_a_cassette_miss(tmp_path):
    gw, _ = gateway([ok(MATCH_OUT)], recorder=CassetteRecorder(tmp_path))
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    player = CassettePlayer.from_paths(tmp_path)
    with pytest.raises(CassetteMiss, match="recorded match"):
        player.call(RoleTask.PLAN_QUERIES, INPUTS[RoleTask.PLAN_QUERIES], episode_id="ep")
    player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    with pytest.raises(CassetteMiss, match="not on the cassette"):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    with pytest.raises(CassetteMiss, match="no cassette"):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="unknown")


def test_cassette_files_are_readable_json(tmp_path):
    gw, _ = gateway([ok(MATCH_OUT)], recorder=CassetteRecorder(tmp_path))
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    text = (tmp_path / "ep.json").read_text()
    assert text.count("\n") > 20  # indented, one field per line
    entry = json.loads(text)["entries"][0]
    assert {"seq", "task", "slot", "role", "model", "effort", "input", "output", "usage"} <= set(
        entry
    )
    assert entry["input"]["trigger"]["headline"] == MATCH_IN.trigger.headline


SCORE_OUT = OUTPUTS[RoleTask.SCORE]
ONE_SCORE = SCORE_OUT.model_copy(update={"scores": SCORE_OUT.scores[:1]})  # s1 only, s2 missing


def cached_outputs(directory) -> list[dict]:
    return [json.loads(p.read_text())["output"] for p in directory.glob("*.json")]


def test_a_score_answer_missing_a_scenario_gets_the_repair_retry(tmp_path):
    gw, backend = gateway([ok(ONE_SCORE), ok(SCORE_OUT)], cache_dir=tmp_path)
    result = gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert result.output == SCORE_OUT and result.meta.repaired
    assert len(backend.requests) == 2
    repair_text = backend.requests[1]["user"]
    assert "s1" in repair_text and "s2" in repair_text
    assert cached_outputs(tmp_path) == [SCORE_OUT.model_dump(mode="json")]


def test_a_score_answer_missing_a_scenario_twice_is_a_model_output_error(tmp_path):
    gw, backend = gateway([ok(ONE_SCORE), ok(ONE_SCORE)], cache_dir=tmp_path)
    with pytest.raises(ModelOutputError):
        gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert len(backend.requests) == 2
    assert cached_outputs(tmp_path) == []


def test_a_cached_score_answer_missing_a_scenario_is_not_served(tmp_path):
    from pmfa.models.cache import CachedAnswer, ResponseCache, cache_key
    from pmfa.models.roles.prompts import render
    from pmfa.records.roles import TASKS

    config = make_config()
    assignment = config.models.slots[TASKS[RoleTask.SCORE].default_slot]
    prompt = render(RoleTask.SCORE, SCORE_IN)
    key = cache_key(
        model=assignment.model,
        effort=assignment.effort,
        max_tokens=assignment.max_tokens,
        system=prompt.system,
        user=prompt.user,
        output_type=ScoreOutput,
    )
    ResponseCache(tmp_path).put(  # as a cache made before the gateway checked it holds one
        CachedAnswer(
            key=key,
            task="score",
            model=assignment.model,
            served_model=assignment.model,
            effort=assignment.effort,
            output=ONE_SCORE.model_dump(mode="json"),
            input_tokens=1000,
            output_tokens=200,
            cost_usd=Decimal("0.01"),
            latency_ms=10,
        )
    )
    gw, backend = gateway([ok(SCORE_OUT)], config=config, cache_dir=tmp_path)
    result = gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert result.output == SCORE_OUT and result.meta.source == "live"
    assert len(backend.requests) == 1
    assert cached_outputs(tmp_path) == [SCORE_OUT.model_dump(mode="json")]


def test_a_complete_score_answer_is_taken_as_it_is(tmp_path):
    gw, backend = gateway([ok(SCORE_OUT)], cache_dir=tmp_path)
    result = gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert result.output == SCORE_OUT and not result.meta.repaired
    assert len(backend.requests) == 1
    assert cached_outputs(tmp_path) == [SCORE_OUT.model_dump(mode="json")]


def test_the_retry_of_a_score_answer_missing_a_scenario_also_needs_budget():
    worst = score_worst_case(make_config())
    cap = str(worst + Decimal("0.05"))  # room for the first call's worst case, not a second
    config = make_config(budget={"max_usd_per_episode": cap, "max_usd_per_run": "25"})
    gw, backend = gateway([ok(ONE_SCORE, tokens=(40_000, 2_000)), ok(SCORE_OUT)], config=config)
    with pytest.raises(BudgetExhausted, match="dollar"):
        gw.call(RoleTask.SCORE, SCORE_IN, episode_id="e")
    assert len(backend.requests) == 1


def test_a_tapped_call_that_failed_after_being_charged_replays_its_charge():
    """The tap records what the gateway charged for a failed call, so its trace replays the same."""
    from pmfa.models.cassette import ModelTap

    worst = score_worst_case(make_config())
    cap = str(worst + Decimal("0.05"))  # the repair retry is refused after a charged attempt
    config = make_config(budget={"max_usd_per_episode": cap, "max_usd_per_run": "25"})
    answers = [ok(MATCH_OUT), raw("a"), raw("b"), raw("bad", tokens=(40_000, 2_000))]
    gw, _ = gateway(answers, config=config, timer=Ticker(1.5))
    tap = ModelTap(gw, config)
    tap.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(ModelOutputError):
        tap.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(BudgetExhausted):
        tap.call(RoleTask.SCORE, SCORE_IN, episode_id="b")

    player = CassettePlayer({e: tap.cassette(e) for e in ("e", "b")})
    player.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(ModelOutputError):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(BudgetExhausted):
        player.call(RoleTask.SCORE, SCORE_IN, episode_id="b")
    assert player.usage("e") == gw.usage("e") and gw.usage("e").calls == 3
    assert player.usage("b") == gw.usage("b") and gw.usage("b").calls == 1


def test_a_transport_error_is_a_model_output_error_and_is_charged():
    """An error from the API client (after its own retries) ends the episode, not the run."""
    from pmfa.errors import ModelCallFailed

    def times_out(request):  # type: ignore[no-untyped-def]
        raise TimeoutError("request timed out after the SDK's retries")

    gw, _ = gateway([times_out, ok(MATCH_OUT)], timer=Ticker(2.0))
    with pytest.raises(ModelCallFailed, match="TimeoutError") as raised:
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert isinstance(raised.value, ModelOutputError)
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert gw.usage("e").calls == 1 and gw.usage("e").seconds == 2.0
    assert gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e").output == MATCH_OUT

    def interrupted(request):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    gw, _ = gateway([interrupted])
    with pytest.raises(KeyboardInterrupt):  # not an error of the call: it passes through
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
