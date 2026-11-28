"""The scoring pass settles outcomes before each scoring point, as the trading pass does.

The tiny world's Fed event settles on Aug 20, between the CPI event's seven-day
point (Aug 18) and its three-day point (Aug 22). So the pipeline's CPI episodes
at three days and one day before close can see the Fed forecasts' outcomes, and
every earlier episode sees none. The boundary tests move the Fed settlement to
one second before the CPI three-day point's as-of time, onto it, and one second
after it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pmfa.config import Config
from pmfa.evaluation.resolver import RESOLUTION_NODE
from pmfa.models import ScriptedModels
from pmfa.records.roles import LedgerExample, RoleTask, ScoreInput
from pmfa.state.store import SqliteStore
from pmfa.world.snapshot.store import SnapshotStore, snapshot_path
from tests.evaluation.backtest.support import (
    CPI,
    CPI_CLOSE,
    FED,
    RESULTS,
    build_data,
    fed_event,
    make_config,
    scripted,
)

CPI_3D = CPI_CLOSE - timedelta(days=3)
CPI_1D = CPI_CLOSE - timedelta(days=1)
FED_QUESTIONS = {f"{m.title} ({m.subtitle})": m.ticker for m in fed_event().markets}
"""The question a Fed ledger entry carries -> its market."""


@dataclass
class Scored:
    """One pipeline-only scoring run: its directory, its models, its episodes by trigger."""

    run_dir: Path
    models: ScriptedModels
    episodes: dict[str, dict[str, Any]]

    def critic_inputs(self, trigger_id: str) -> list[ScoreInput]:
        """What the Critic was given in the episode of ``trigger_id``, one per call."""
        episode_id = self.episodes[trigger_id]["episode_id"]
        return [
            c.payload  # type: ignore[misc]
            for c in self.models.calls
            if c.task is RoleTask.SCORE and c.episode_id == episode_id
        ]

    def examples(self, trigger_id: str) -> list[LedgerExample]:
        """Every calibration example the Critic was given in the episode of ``trigger_id``."""
        return [x for i in self.critic_inputs(trigger_id) for x in i.ledger_examples]

    def resolution_times(self, episode_ids: list[str]) -> set[datetime]:
        """When the resolver wrote each episode's resolution observation (its clock's time)."""
        with SqliteStore(self.run_dir / "work" / "scoring.sqlite") as store:
            return {
                step.at
                for episode_id in episode_ids
                for step in store.get_trace(episode_id)
                if step.node == RESOLUTION_NODE
            }

    def equity_times(self) -> set[datetime]:
        with SqliteStore(self.run_dir / "work" / "scoring.sqlite") as store:
            return {t for t, _ in store.equity_series()}


def _point(event: str, days: int) -> str:
    return f"schedule:{event}:{days}d"


def _config(root: Path, *, strict_calibration: bool = False) -> Config:
    config = make_config(root / "data")
    if strict_calibration:
        # One resolved forecast is enough, any error is too much, and only "now" is checked.
        config = config.model_copy(
            update={
                "routing": config.routing.model_copy(
                    update={"calibration_min_resolved": 1, "calibration_error_max": 0.0}
                ),
                "oversight": config.oversight.model_copy(update={"calibration_halt_weeks": 0}),
            }
        )
    return config


def _build(config: Config, *, fed_settles_at: datetime | None = None) -> None:
    """The tiny world, with the Fed markets' settlement time moved when asked."""
    build_data(config)
    if fed_settles_at is None:
        return
    event = fed_event()
    moved = event.model_copy(
        update={
            "markets": [m.model_copy(update={"settled_at": fed_settles_at}) for m in event.markets]
        }
    )
    store = SnapshotStore(snapshot_path(config))
    store.put_event(moved)
    store.close()


def _score(config: Config, run_dir: Path, *, save_examples: int = 0) -> Scored:
    from pmfa.evaluation.backtest import run_backtest

    models = scripted(config)
    run_backtest(
        config,
        run_dir=run_dir,
        systems=["pipeline"],
        models=models,
        trade=False,
        save_examples=save_examples,
    )
    lines = (run_dir / "episodes.jsonl").read_text().splitlines()
    episodes = {e["trigger"]["trigger_id"]: e for e in map(json.loads, lines)}
    return Scored(run_dir, models, episodes)


@pytest.fixture(scope="module")
def scored(tmp_path_factory: pytest.TempPathFactory) -> Scored:
    root = tmp_path_factory.mktemp("scored")
    config = _config(root)
    _build(config)
    return _score(config, root / "run", save_examples=6)


# --------------------------------------------------------------------------- what the critic sees


def test_a_point_after_an_event_settled_gives_the_critic_its_outcomes(scored: Scored) -> None:
    for before in (_point(FED, 7), _point(FED, 3), _point(CPI, 7), _point(FED, 1)):
        assert scored.critic_inputs(before), before
        assert scored.examples(before) == [], before
    fed_rows = _pipeline_rows(scored.run_dir, FED)
    for after in (_point(CPI, 3), _point(CPI, 1)):
        examples = scored.examples(after)
        assert examples, after
        for x in examples:
            # Only the Fed forecasts had resolved; each example is one of them, with its outcome.
            ticker = FED_QUESTIONS[x.question]
            assert x.outcome == (RESULTS[ticker].value == "yes")
            assert x.forecast_probability in fed_rows[ticker]


def _pipeline_rows(run_dir: Path, event: str) -> dict[str, set[float]]:
    """The pipeline's scoring probabilities for each market of ``event``."""
    from pmfa.evaluation.backtest import read_forecasts

    out: dict[str, set[float]] = {}
    for row, _ in read_forecasts(run_dir / "forecasts.csv"):
        if row.system == "pipeline" and row.event_ticker == event and row.probability is not None:
            out.setdefault(row.market_ticker, set()).add(row.probability)
    return out


def test_the_scoring_pass_settles_the_paper_account_at_the_first_point_after_settlement(
    scored: Scored,
) -> None:
    fed_episodes = [scored.episodes[_point(FED, d)]["episode_id"] for d in (7, 3, 1)]
    # Resolved at the CPI three-day point, the first point after the Fed settled, not at the end.
    assert scored.resolution_times(fed_episodes) == {CPI_3D}
    with SqliteStore(scored.run_dir / "work" / "scoring.sqlite") as store:
        settled = {t for t, _, _ in store.list_settlements()}
        traded = {o.market_ticker for o in store.list_orders() if o.filled_contracts}
    fed_traded = {t for t in traded if t.startswith(FED)}
    assert fed_traded and fed_traded <= settled
    assert CPI_3D in scored.equity_times()  # the settlement was marked at that point


def test_every_saved_scoring_trace_replays_including_one_after_a_settlement(
    scored: Scored,
) -> None:
    from pmfa.evaluation.manifest import read_manifest
    from pmfa.world.recorded import RecordedWorldFile

    traces = read_manifest(scored.run_dir).traces
    assert len(traces) == 6
    assert set(traces.values()) == {"replays and matches"}
    cpi_3d = scored.episodes[_point(CPI, 3)]["episode_id"]
    [folder] = [name for name in traces if name.endswith(cpi_3d)]
    world = RecordedWorldFile.model_validate_json(
        (scored.run_dir / folder / "world.json").read_text()
    )
    # The recording started from a store that held resolved Fed entries and a settlement.
    assert world.ledger and all(e.outcome is not None for e in world.ledger)
    assert world.settlements
    assert all(s.market_ticker.startswith(FED) for s in world.settlements)
    assert all(e.resolved_at is not None and e.resolved_at <= world.as_of for e in world.ledger)


# --------------------------------------------------------------------------- the as-of boundary


@pytest.mark.parametrize(
    ("offset", "resolved_at", "critic_at_3d"),
    [
        (timedelta(seconds=-1), CPI_3D, True),
        (timedelta(0), CPI_3D, False),
        (timedelta(seconds=1), CPI_1D, False),
    ],
    ids=["one-second-before", "at-the-as-of-time", "one-second-after"],
)
def test_a_result_settled_near_a_point_reaches_the_account_and_the_critic_as_the_ports_allow(
    tmp_path: Path, offset: timedelta, resolved_at: datetime, critic_at_3d: bool
) -> None:
    """The Fed settles one second before, at, or one second after the CPI three-day point.

    The resolver writes back what the exchange shows at the point (settled at
    or before it), so the paper account settles it there. The Critic's examples
    and the calibration stop count only results resolved before the point, so
    one settled at the point reaches them at the next point, two days later.
    """
    config = _config(tmp_path, strict_calibration=True)
    _build(config, fed_settles_at=CPI_3D + offset)
    run = _score(config, tmp_path / "run")
    fed_episodes = [run.episodes[_point(FED, d)]["episode_id"] for d in (7, 3, 1)]

    # The resolver and the paper account: at the first point the exchange shows the result.
    assert run.resolution_times(fed_episodes) == {resolved_at}
    assert (CPI_3D in run.equity_times()) is (resolved_at == CPI_3D)
    with SqliteStore(run.run_dir / "work" / "scoring.sqlite") as store:
        fed_settled = [at for t, _, at in store.list_settlements() if t.startswith(FED)]
        stop_at_3d = store.calibration("fed_decision", as_of=CPI_3D).n_resolved
        stop_at_1d = store.calibration("fed_decision", as_of=CPI_1D).n_resolved
    assert fed_settled and set(fed_settled) == {CPI_3D + offset}

    # The Critic and the calibration stop at the three-day point: resolved before it only.
    at_3d = run.episodes[_point(CPI, 3)]
    assert bool(run.examples(_point(CPI, 3))) is critic_at_3d
    assert (stop_at_3d > 0) is critic_at_3d
    halted = at_3d["exit"] == "halted" and "calibration" in at_3d["exit_detail"]
    assert halted is critic_at_3d

    # At the next point, two days later, every variant gives both the Fed outcomes.
    assert run.examples(_point(CPI, 1))
    assert stop_at_1d > 0


# --------------------------------------------------------------------------- a halt and the scores


def test_a_calibration_halt_in_the_scoring_pass_leaves_every_score_in_place(
    tmp_path: Path, scored: Scored
) -> None:
    config = _config(tmp_path, strict_calibration=True)
    _build(config)
    halted = _score(config, tmp_path / "halted")

    metrics = json.loads((halted.run_dir / "metrics.json").read_text())
    block = metrics["self_halts"]["scoring"]
    assert block["first_trigger_id"] == _point(CPI, 3)
    assert block["reason"].startswith("self-halt: calibration")
    assert block["n_halted_from_then"] == 2
    assert (
        json.loads((scored.run_dir / "metrics.json").read_text())["self_halts"]["scoring"] is None
    )

    # Every point still has its forecast and its rows, the same as in the run that never halted.
    def rows(run: Scored) -> list[tuple[Any, ...]]:
        from pmfa.evaluation.backtest import read_forecasts

        return [
            (r.event_ticker, r.market_ticker, r.horizon_days, r.as_of, r.probability, r.outcome)
            for r, _ in read_forecasts(run.run_dir / "forecasts.csv")
        ]

    assert len(rows(halted)) == 18
    assert all(r[4] is not None for r in rows(halted))
    assert rows(halted) == rows(scored)


# --------------------------------------------------------------------------- without the pipeline


@pytest.mark.parametrize("systems", [["market"], ["market", "prompt_only", "single_pass"]])
def test_a_run_without_the_pipeline_never_calls_the_resolver(
    data_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, systems: list[str]
) -> None:
    import pmfa.evaluation.backtest as backtest

    def resolve(world: Any, *, now: datetime | None = None) -> Any:
        raise AssertionError("the resolver ran in a run without the pipeline")

    monkeypatch.setattr(backtest, "resolve_outcomes", resolve)
    manifest = backtest.run_backtest(
        data_config, run_dir=tmp_path / "run", systems=systems, models=scripted(data_config)
    )
    assert manifest.status == "complete"
