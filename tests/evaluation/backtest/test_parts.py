"""The parts of the backtest on their own: schedule, resolver, baselines conversion, report, publishing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.evaluation.baselines import outcome_distribution
from pmfa.evaluation.resolver import resolve_outcomes
from pmfa.evaluation.schedule import subsample
from pmfa.forecast import build_outcome_space
from pmfa.records.base import RunMode
from pmfa.records.episode import LedgerEntry
from pmfa.records.roles import BaselineForecast, OptionProbability
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.backtest import BacktestData, build_backtest_world
from pmfa.world.clocks import SimClock
from tests.evaluation.backtest.conftest import FullRun
from tests.evaluation.backtest.support import FED, fed_event, scripted


def test_subsample_is_even_and_keeps_the_ends() -> None:
    kept, dropped = subsample(list(range(10)), 4)
    assert kept == [0, 3, 6, 9]
    assert dropped == [1, 2, 4, 5, 7, 8]
    assert subsample([1, 2], None) == ([1, 2], [])
    assert subsample([1, 2, 3], 1) == ([1], [2, 3])


def test_schedule_is_deterministic(data_config: Config) -> None:
    from pmfa.evaluation.schedule import build_schedule

    data = BacktestData(data_config)
    a = build_schedule(data.events(), data.corpus, data_config, window_end=data.window_end)
    b = build_schedule(data.events(), data.corpus, data_config, window_end=data.window_end)
    assert a == b
    assert [p.point_id for p in a.points] == [
        f"{FED}@7d",
        f"{FED}@3d",
        "KXCPIYOY-26AUG@7d",
        f"{FED}@1d",
        "KXCPIYOY-26AUG@3d",
        "KXCPIYOY-26AUG@1d",
    ]
    assert [t.at for t in a.triggers] == sorted(t.at for t in a.triggers)
    data.close()


def _entry(ticker: str, probability: float, price: float) -> LedgerEntry:
    return LedgerEntry(
        entry_id=f"e-{ticker}",
        episode_id="ep-1",
        mode=RunMode.BACKTEST,
        event_ticker=FED,
        market_ticker=ticker,
        category="fed_decision",
        question=ticker,
        as_of=datetime(2026, 8, 13, tzinfo=UTC),
        probability=probability,
        market_price=price,
    )


def test_resolver_resolves_only_what_has_settled(data_config: Config) -> None:
    data = BacktestData(data_config)
    clock = SimClock(datetime(2026, 8, 19, tzinfo=UTC))
    store = SqliteStore(MEMORY)
    store.add_ledger_entries([_entry(f"{FED}-H0", 0.9, 0.6), _entry(f"{FED}-C25", 0.05, 0.3)])
    world = build_backtest_world(
        data, data_config, clock=clock, store=store, models=scripted(data_config)
    )
    before = resolve_outcomes(world)
    assert before.entries_resolved == 0
    assert sorted(before.unresolved) == [f"{FED}-C25", f"{FED}-H0"]

    clock.set(datetime(2026, 8, 21, tzinfo=UTC))
    after = resolve_outcomes(world)
    assert after.entries_resolved == 2
    entries = {e.market_ticker: e for e in store.ledger()}
    assert entries[f"{FED}-H0"].outcome is True
    assert entries[f"{FED}-H0"].brier == pytest.approx(0.01)
    assert entries[f"{FED}-H0"].market_brier == pytest.approx(0.16)
    assert entries[f"{FED}-C25"].brier == pytest.approx(0.0025)
    assert after.entries_indexed == 2
    assert resolve_outcomes(world).entries_resolved == 0  # idempotent
    world.knowledge.close()  # type: ignore[attr-defined]
    data.close()


def test_baseline_answers_are_converted_or_refused(data_config: Config) -> None:
    sc = data_config.follow.for_ticker("KXFEDDECISION")
    assert sc is not None
    space = build_outcome_space(fed_event(), sc)
    good = BaselineForecast(
        option_probabilities=[
            OptionProbability(outcome_id="hold", probability=2.0),
            OptionProbability(outcome_id="cut_25", probability=2.0),
        ],
        quantiles=None,
        rationale="",
    )
    dist, problem = outcome_distribution(good, space)
    assert problem == "" and dist is not None
    assert dist["hold"] == pytest.approx(0.5) and dist["hike_25"] == 0.0
    bad = good.model_copy(
        update={"option_probabilities": [OptionProbability(outcome_id="pause", probability=1.0)]}
    )
    dist, problem = outcome_distribution(bad, space)
    assert dist is None and "unknown options" in problem


def test_report_builds_with_every_figure(full_run: FullRun) -> None:
    from pmfa.evaluation.report import FIGURES, build_report

    text = build_report(full_run.run_dir).read_text()
    for heading in (
        "## What was run and what was not",
        "## Sample sizes",
        "## Brier score and Brier skill against the market",
        "## Reliability",
        "## Simulated trading after fees",
        "## Routing tiers",
        "## Exits by reason",
        "## Gate rejections by reason",
        "## Cost, calls and latency per episode",
        "## Pruning audit",
        "## How each fill was priced",
        "## How look-ahead was prevented",
        "## Fill assumptions",
        "## Limitations",
    ):
        assert heading in text, heading
    assert "pilot" in text
    for name in FIGURES:
        assert (full_run.run_dir / "figures" / name).stat().st_size > 0


def test_publish_copies_exactly_the_committed_set(full_run: FullRun, tmp_path: Path) -> None:
    from pmfa.evaluation.report import build_report, publish_run

    build_report(full_run.run_dir)
    target = publish_run(full_run.run_dir, "2026-08-31-test", root=tmp_path / "evaluation" / "runs")
    published = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    top = {p for p in published if "/" not in p}
    assert top == {"manifest.json", "forecasts.csv", "metrics.json", "report.md"}
    assert {p.split("/")[0] for p in published} == {
        "manifest.json",
        "forecasts.csv",
        "metrics.json",
        "report.md",
        "figures",
        "traces",
    }
    source_traces = {
        p.relative_to(full_run.run_dir).as_posix()
        for p in (full_run.run_dir / "traces").rglob("*")
        if p.is_file()
    }
    assert {p for p in published if p.startswith("traces/")} == source_traces
    assert not any(p.startswith(("work", "trades", "episodes")) for p in published)
