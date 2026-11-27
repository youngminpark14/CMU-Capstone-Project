"""The backtest runner end to end on the tiny frozen world, with scripted models."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.errors import ConfigError
from pmfa.evaluation.backtest import read_forecasts, run_backtest
from pmfa.evaluation.manifest import read_manifest
from pmfa.evaluation.resolver import RESOLUTION_NODE
from pmfa.records.episode import EpisodeRecord
from pmfa.records.evidence import Passage, RetrievedPassage
from pmfa.state.store import SqliteStore
from pmfa.world.backtest import LeakageDetected
from tests.evaluation.backtest.conftest import FullRun
from tests.evaluation.backtest.support import (
    ALL_SYSTEMS,
    CPI,
    FED,
    RESULTS,
    CappedModels,
    make_config,
    scripted,
)

# --------------------------------------------------------------------------- the full run


def test_every_system_forecasts_every_point(full_run: FullRun) -> None:
    rows = [r for r, _ in read_forecasts(full_run.run_dir / "forecasts.csv")]
    scheduled = [r for r in rows if r.horizon_days is not None]
    for system in ALL_SYSTEMS:
        mine = [r for r in scheduled if r.system == system]
        assert {(r.event_ticker, r.horizon_days) for r in mine} == {
            (e, h) for e in (FED, CPI) for h in (7, 3, 1)
        }, system
        assert len(mine) == 18  # 6 points x 3 listed markets
        assert all(r.probability is not None for r in mine), system
    assert full_run.manifest.status == "complete"
    assert all(v == "run" for v in full_run.manifest.systems.values())


def test_outcomes_come_from_the_snapshot_results(full_run: FullRun) -> None:
    for row, _ in read_forecasts(full_run.run_dir / "forecasts.csv"):
        assert row.outcome == (RESULTS[row.market_ticker].value == "yes")


def test_market_rows_are_the_market_price(full_run: FullRun) -> None:
    rows = [
        r for r, _ in read_forecasts(full_run.run_dir / "forecasts.csv") if r.system == "market"
    ]
    assert rows and all(r.probability == r.market_price for r in rows)
    hold = [r for r in rows if r.market_ticker == f"{FED}-H0"]
    assert {r.market_price for r in hold} == {0.6}


def test_trading_pass_carries_holdings_and_settles_them(full_run: FullRun) -> None:
    with (full_run.run_dir / "trades.csv").open() as fh:
        trades = list(csv.DictReader(fh))
    assert trades, "the scripted pipeline should have traded"
    assert {t["market_ticker"] for t in trades} >= {f"{FED}-H0"}
    assert all(t["result"] and t["settled_at"] for t in trades)
    assert all(t["fill_tagged"] == "true" and t["tier"] == "approve" for t in trades)
    assert {t["pricing"] for t in trades} == {"minute", "hour"}

    store = SqliteStore(full_run.run_dir / "work" / "trading.sqlite")
    settled = {t for t, _, _ in store.list_settlements()}
    assert f"{FED}-H0" in settled
    # The bankroll carried forward: the equity series starts at the bankroll and ends above it.
    equity = store.equity_series()
    assert equity[0][1] == full_run.config.execution.notional_bankroll
    metrics = json.loads((full_run.run_dir / "metrics.json").read_text())
    assert metrics["pnl"]["summary"]["n_settled"] == len(trades)
    store.close()


def test_resolver_writes_brier_scores_and_the_pruning_audit(full_run: FullRun) -> None:
    store = SqliteStore(full_run.run_dir / "work" / "trading.sqlite")
    ledger = store.ledger()
    assert ledger and all(e.outcome is not None and e.brier is not None for e in ledger)
    for e in ledger:
        assert e.outcome == (RESULTS[e.market_ticker].value == "yes")
        assert e.brier == pytest.approx((e.probability - float(e.outcome)) ** 2)
    audited = [
        e for e in store.list_episodes() if any(step.node == RESOLUTION_NODE for step in e.trace)
    ]
    assert audited, "every resolved episode with a tree gets a resolution observation"
    metrics = json.loads((full_run.run_dir / "metrics.json").read_text())
    assert metrics["pruning"]["summary"]["n_episodes"] >= len(audited)
    store.close()


def test_each_pruning_audit_is_counted_once_across_the_resolves(full_run: FullRun) -> None:
    """Both passes resolve before their episodes and again at the end; an audit is made once."""
    metrics = json.loads((full_run.run_dir / "metrics.json").read_text())
    counted = [a["episode_id"] for a in metrics["pruning"]["episodes"]]
    assert len(counted) == len(set(counted))
    audited: dict[str, dict[str, list[datetime]]] = {}
    for name in ("scoring.sqlite", "trading.sqlite"):
        store = SqliteStore(full_run.run_dir / "work" / name)
        audited[name] = {}
        for e in store.list_episodes():
            steps = [s.at for s in store.get_trace(e.episode_id) if s.node == RESOLUTION_NODE]
            if steps:
                audited[name][e.episode_id] = steps
        store.close()
    every = {**audited["scoring.sqlite"], **audited["trading.sqlite"]}
    assert sorted(counted) == sorted(every)
    assert all(len(times) == 1 for times in every.values())
    assert metrics["pruning"]["summary"]["n_episodes"] == len(counted)


@pytest.mark.parametrize("store_name", ["scoring.sqlite", "trading.sqlite"])
def test_each_pass_starts_with_the_bankroll_at_the_windows_start(
    full_run: FullRun, store_name: str
) -> None:
    """The daily loss stop and the drawdown halt measure from the equity points. Each pass
    writes the first one before any order, so the stops measure from the bankroll and the
    first trade's spread and fee count against them."""
    from pmfa.evaluation.schedule import window_start_time

    store = SqliteStore(full_run.run_dir / "work" / store_name)
    series = store.equity_series()
    orders = store.list_orders()
    store.close()
    start = window_start_time(full_run.config.evaluation.window_start)
    assert series[0] == (start, full_run.config.execution.notional_bankroll)
    assert orders and series[0][0] < min(o.placed_at for o in orders)


def test_the_report_says_what_the_scoring_passs_rows_mean_only_when_it_has_some(
    full_run: FullRun, data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.backtest import run_backtest
    from pmfa.evaluation.report import SCORING_PASS_NOTE, build_report

    text = build_report(full_run.run_dir).read_text(encoding="utf-8")
    tiers = text.split("## Routing tiers", 1)[1].split("\n## ", 1)[0]
    assert SCORING_PASS_NOTE in tiers
    # A market-only run has no pipeline episode in either pass, so there is nothing to explain.
    run_backtest(data_config, run_dir=tmp_path / "market", systems=["market"])
    assert SCORING_PASS_NOTE not in build_report(tmp_path / "market").read_text(encoding="utf-8")


def test_outputs_and_traces(full_run: FullRun) -> None:
    run_dir = full_run.run_dir
    for name in ("manifest.json", "forecasts.csv", "trades.csv", "episodes.jsonl", "metrics.json"):
        assert (run_dir / name).is_file(), name
    episodes = [
        EpisodeRecord.model_validate_json(line)
        for line in (run_dir / "episodes.jsonl").read_text().splitlines()
    ]
    assert len(episodes) == 6 + 3  # six scoring points, three news triggers
    assert all(e.trace == [] for e in episodes)
    traces = sorted(p.name for p in (run_dir / "traces").iterdir())
    assert len(traces) == 3
    assert traces[0].startswith("01-executed")
    for name in traces:
        assert (run_dir / "traces" / name / "world.json").is_file()
        assert (run_dir / "traces" / name / "cassette.json").is_file()
    manifest = read_manifest(run_dir)
    assert set(manifest.traces) == {f"traces/{n}" for n in traces}


def test_every_saved_trace_replays_offline_to_its_expected_record(full_run: FullRun) -> None:
    from pmfa.examples import check, replay

    manifest = read_manifest(full_run.run_dir)
    assert len(manifest.traces) == 3
    for relative, status in manifest.traces.items():
        result = replay(full_run.run_dir / relative)
        assert check(result) == [], relative
        assert status == "replays and matches", relative
        # The expected record is the episode at its exit: the resolver's later
        # observation is not part of what a replay can reproduce.
        expected = EpisodeRecord.model_validate_json(
            (full_run.run_dir / relative / "expected" / "episode.json").read_text()
        )
        assert all(step.node != RESOLUTION_NODE for step in expected.trace), relative
        assert result.episode.exit == expected.exit, relative


def test_manifest_records_what_produced_the_numbers(full_run: FullRun) -> None:
    m = full_run.manifest
    assert m.models["baseline"].model == "claude-fable-5-2"
    assert str(m.models["baseline"].knowledge_cutoff) == "2026-06-30"
    assert m.plan.scoring_points == 6 and m.plan.triggers == 3
    assert m.seed == full_run.config.evaluation.seed
    assert m.config["state"]["transport"] == "direct"
    assert m.snapshot_manifest["counts"]["events"] == 2
    assert m.leakage_controls and m.cost_controls and m.fill_assumptions
    assert m.models_source == "supplied"


def test_canary_is_never_retrieved(full_run: FullRun) -> None:
    payloads = [c.payload.model_dump_json() for c in full_run.models.calls]
    assert payloads
    assert not any("CANARY" in p for p in payloads)


# --------------------------------------------------------------------------- refusals and stops


def test_canary_in_a_retrieval_result_aborts_the_run(
    data_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pmfa.knowledge.retriever import ChromaKnowledge
    from pmfa.world.backtest import BacktestData

    canary = BacktestData(data_config).canary
    real = ChromaKnowledge.search_evidence

    def leaky(self, queries, **kwargs):  # type: ignore[no-untyped-def]
        hits = real(self, queries, **kwargs)
        planted = Passage(
            passage_id=f"{canary.doc_id}#0",
            doc_id=canary.doc_id,
            origin_id=canary.doc_id,
            source=canary.source,
            url=canary.url,
            title=canary.title,
            published_at=canary.published_at,
            index=0,
            text=canary.text,
        )
        return [*hits, RetrievedPassage(passage=planted, similarity=0.9, recency=1.0, score=0.9)]

    monkeypatch.setattr(ChromaKnowledge, "search_evidence", leaky)
    run_dir = tmp_path / "runs" / "leak"
    with pytest.raises(LeakageDetected):
        run_backtest(
            data_config,
            run_dir=run_dir,
            systems=["market", "single_pass"],
            models=scripted(data_config),
        )
    assert read_manifest(run_dir).status == "aborted"


def _plant_the_canary(data_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every evidence search return the canary as well, as a broken as-of filter would."""
    from pmfa.knowledge.retriever import ChromaKnowledge
    from pmfa.world.backtest import BacktestData

    canary = BacktestData(data_config).canary
    real = ChromaKnowledge.search_evidence

    def leaky(self, queries, **kwargs):  # type: ignore[no-untyped-def]
        planted = Passage(
            passage_id=f"{canary.doc_id}#0",
            doc_id=canary.doc_id,
            origin_id=canary.doc_id,
            source=canary.source,
            url=canary.url,
            title=canary.title,
            published_at=canary.published_at,
            index=0,
            text=canary.text,
        )
        hit = RetrievedPassage(passage=planted, similarity=0.9, recency=1.0, score=0.9)
        return [*real(self, queries, **kwargs), hit]

    monkeypatch.setattr(ChromaKnowledge, "search_evidence", leaky)


def test_leakage_is_an_as_of_violation() -> None:
    from pmfa.errors import AsOfViolation

    assert issubclass(LeakageDetected, AsOfViolation)


def test_canary_in_a_pipeline_retrieval_aborts_the_run(
    data_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pipeline turns most project errors into an episode's exit `error`; a leak must
    # not become one. The scan of finished episodes is switched off, so only the tripwire
    # inside retrieval, raised in the pipeline's own node, can abort the run.
    _plant_the_canary(data_config, monkeypatch)
    scanned: list[str] = []

    def no_scan(episode, **kwargs):  # type: ignore[no-untyped-def]
        scanned.append(episode.episode_id)

    monkeypatch.setattr("pmfa.evaluation.backtest.assert_episode_clean", no_scan)
    run_dir = tmp_path / "runs" / "leak-pipeline"
    with pytest.raises(LeakageDetected):
        run_backtest(
            data_config, run_dir=run_dir, systems=["pipeline"], models=scripted(data_config)
        )
    assert read_manifest(run_dir).status == "aborted"
    assert scanned == []  # no episode finished: the first leaking retrieval stopped the run


def test_the_index_key_names_the_stored_row_format(
    data_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pmfa.world import backtest as backtest_world

    before = backtest_world.BacktestData(data_config)
    monkeypatch.setattr(backtest_world, "INDEX_FORMAT", backtest_world.INDEX_FORMAT + 1)
    after = backtest_world.BacktestData(data_config)
    assert after.index_key != before.index_key
    assert after.index_path != before.index_path
    before.close()
    after.close()


def test_a_model_cutoff_inside_the_window_refuses_to_start(
    data_config: Config, tmp_path: Path
) -> None:
    catalog = dict(data_config.models.catalog)
    haiku = catalog["claude-haiku-5-5"]
    catalog["claude-haiku-5-5"] = haiku.model_copy(
        update={"knowledge_cutoff": data_config.evaluation.window_start}
    )
    config = data_config.model_copy(
        update={"models": data_config.models.model_copy(update={"catalog": catalog})}
    )
    models = scripted(config)
    run_dir = tmp_path / "runs" / "leaky-window"
    with pytest.raises(ConfigError, match="knowledge cutoff"):
        run_backtest(config, run_dir=run_dir, systems=ALL_SYSTEMS, models=models)
    assert not (run_dir / "manifest.json").exists()
    assert models.calls == []


def test_dollar_cap_stops_cleanly_and_marks_the_run_partial(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import FIGURES, build_report

    run_dir = tmp_path / "runs" / "capped"
    models = CappedModels(scripted(data_config), allowed=7)
    manifest = run_backtest(data_config, run_dir=run_dir, systems=ALL_SYSTEMS, models=models)
    assert manifest.status == "partial"
    assert "dollar cap" in manifest.progress.stopped_reason
    assert manifest.progress.points_done and manifest.progress.points_not_run
    assert set(manifest.progress.triggers_not_run) == {
        "doc:fed-statement-2026-08-10",
        "doc:bls-cpi-2026-08-14",
        "doc:fed-speech-2026-08-21",
    }
    # The cap stopped every system part way through the scoring pass, the market too.
    assert manifest.systems["market"].startswith("partial: ")
    assert manifest.systems["pipeline"].startswith("partial: ")
    rows = read_forecasts(run_dir / "forecasts.csv")
    assert len({(r.event_ticker, r.horizon_days, r.system) for r, _ in rows}) == len(
        manifest.progress.points_done
    )
    report = build_report(run_dir)
    assert "partial" in report.read_text()
    for name in FIGURES:
        assert (run_dir / "figures" / name).is_file()


def test_dry_run_makes_no_model_call(data_config: Config, tmp_path: Path) -> None:
    models = scripted(data_config)
    run_dir = tmp_path / "runs" / "dry"
    manifest = run_backtest(
        data_config, run_dir=run_dir, systems=ALL_SYSTEMS, models=models, dry_run=True
    )
    assert models.calls == []
    assert manifest.status == "dry_run"
    assert manifest.plan.scoring_points == 6 and manifest.plan.triggers == 3
    by_system = {p.system: p for p in manifest.plan.systems}
    assert by_system["market"].model_calls_max == 0
    assert by_system["prompt_only"].model_calls_max == 6
    assert by_system["pipeline"].model_calls_max >= by_system["pipeline"].model_calls_min > 0
    assert manifest.plan.usd_max > 0 and manifest.plan.assumptions
    assert not (run_dir / "forecasts.csv").exists()


def test_without_a_key_only_the_market_runs(
    data_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pmfa.evaluation.report import FIGURES, build_report

    monkeypatch.delenv(data_config.models.api_key_env, raising=False)
    run_dir = tmp_path / "runs" / "nokey"
    manifest = run_backtest(data_config, run_dir=run_dir, systems=ALL_SYSTEMS)
    assert manifest.status == "complete"
    assert manifest.systems["market"] == "run"
    for system in ("prompt_only", "single_pass", "single_pass_similarity", "pipeline"):
        assert manifest.systems[system] == "not run: no API key"
    systems = {r.system for r, _ in read_forecasts(run_dir / "forecasts.csv")}
    assert systems == {"market"}
    text = build_report(run_dir).read_text()
    assert "not run: no API key" in text
    assert "no Anthropic API key" in text
    for name in FIGURES:
        assert (run_dir / "figures" / name).is_file()


def test_limit_takes_an_even_subsample_and_says_what_it_left_out(
    data_config: Config, tmp_path: Path
) -> None:
    manifest = run_backtest(
        data_config,
        run_dir=tmp_path / "runs" / "limited",
        systems=["market"],
        limit=2,
        dry_run=True,
    )
    assert manifest.plan.scoring_points == 2
    assert len(manifest.plan.left_out_points) == 4
    assert len(manifest.plan.left_out_triggers) == 1


def test_scoring_and_trading_use_separate_stores(full_run: FullRun) -> None:
    for name in ("scoring.sqlite", "trading.sqlite"):
        with sqlite3.connect(full_run.run_dir / "work" / name) as db:
            kinds = {
                json.loads(row[0])["trigger"]["kind"]
                for row in db.execute("SELECT json FROM episodes")
            }
        assert kinds == ({"schedule"} if name == "scoring.sqlite" else {"new_document"})


def test_unknown_system_is_refused(data_config: Config, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown system"):
        run_backtest(data_config, run_dir=tmp_path / "r", systems=["oracle"], dry_run=True)


def test_window_start_skips_points_before_it(tmp_path: Path) -> None:
    from datetime import date

    from tests.evaluation.backtest.support import build_data

    config = make_config(tmp_path / "data", window_start=date(2026, 8, 15))
    build_data(config)
    manifest = run_backtest(config, run_dir=tmp_path / "r", systems=["market"], dry_run=True)
    assert f"{FED}@7d" in " ".join(manifest.plan.skipped_points)
    assert manifest.plan.scoring_points == 5
