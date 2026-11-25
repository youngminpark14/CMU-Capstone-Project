"""The report names everything the window held that was not scored, and counts what was."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from pmfa.config import Config
from pmfa.evaluation.backtest import run_backtest
from pmfa.records.evidence import ScheduledEvent
from pmfa.world.evidence.corpus import CorpusStore
from pmfa.world.snapshot.store import SnapshotStore, snapshot_path
from tests.evaluation.backtest.support import (
    CALENDAR,
    CPI,
    DOCUMENTS,
    FED,
    build_data,
    cpi_event,
    make_config,
    scripted,
)

SEP_RELEASE = ScheduledEvent(
    scheduled_event_id="cpi_release:2026-07-extra",
    kind="cpi_release",
    title="Consumer Price Index, an extra release",
    scheduled_at=datetime(2026, 8, 30, 12, 30, tzinfo=UTC),
)


def _section(text: str, heading: str) -> str:
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def _add_release(config: Config) -> None:
    corpus = CorpusStore(config.paths.corpus_dir())
    manifest = corpus.manifest()
    assert manifest is not None
    corpus.write(DOCUMENTS, events=[*CALENDAR, SEP_RELEASE], manifest=manifest)


def _set_dropped(config: Config, dropped: dict[str, str] | None) -> None:
    store = SnapshotStore(snapshot_path(config))
    manifest = store.manifest()
    assert manifest is not None
    store.write_manifest(manifest.model_copy(update={"dropped_events": dropped}))
    store.close()


def test_skipped_events_and_the_scored_event_count_are_reported(tmp_path: Path) -> None:
    from pmfa.evaluation.report import build_report

    config = make_config(tmp_path / "data", window_start=date(2026, 8, 21))
    build_data(config)
    run_dir = tmp_path / "run"
    manifest = run_backtest(config, run_dir=run_dir, systems=["market"])
    assert any(line.startswith(FED) for line in manifest.plan.skipped_events)
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["scored_events"] == [CPI]

    text = build_report(run_dir).read_text()
    left_out = _section(text, "Left out of scoring")
    assert f"{FED}: closes" in left_out and "outside the window" in left_out
    limitations = _section(text, "Limitations")
    assert "Scoring covered only 1 settled event(s) (1 KXCPIYOY)" in limitations
    assert "2 settled events" not in text


def test_a_release_without_an_event_is_named_and_qualified(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    _add_release(data_config)
    run_dir = tmp_path / "run"
    manifest = run_backtest(data_config, run_dir=run_dir, systems=["market"])
    assert manifest.plan.releases_without_event == [
        "cpi_release:2026-07-extra (Consumer Price Index, an extra release, "
        "2026-08-30T12:30:00+00:00): no KXCPIYOY event in the snapshot"
    ]
    text = build_report(run_dir).read_text()
    left_out = _section(text, "Left out of scoring")
    assert "no KXCPIYOY event in the snapshot" in left_out
    # The support snapshot predates the record of dropped events.
    assert "built before its builder recorded the events it dropped" in left_out
    assert "1 release(s) in the window had no event" in _section(text, "Limitations")

    _set_dropped(data_config, {})
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert "dropped no event of KXCPIYOY, so the exchange listed no event" in left_out

    _set_dropped(data_config, {f"{CPI}-26SEPX": "listed with no markets"})
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert f"{CPI}-26SEPX: listed with no markets" in left_out
    assert "may belong to one of these releases" in left_out


def test_nothing_left_out_is_said_plainly(data_config: Config, tmp_path: Path) -> None:
    from pmfa.evaluation.report import build_report

    _set_dropped(data_config, {})
    run_dir = tmp_path / "run"
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert "Nothing in the window was left out" in left_out


def test_trading_rows_without_a_final_result_are_counted(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    unsettled = cpi_event()
    markets = list(unsettled.markets)
    markets[2] = markets[2].model_copy(update={"result": None, "settled_at": None})
    store = SnapshotStore(snapshot_path(data_config))
    store.put_event(unsettled.model_copy(update={"markets": markets}))
    store.close()

    run_dir = tmp_path / "run"
    manifest = run_backtest(
        data_config,
        run_dir=run_dir,
        systems=["market", "pipeline"],
        models=scripted(data_config),
        score=False,
    )
    assert f"{CPI}: not settled in the snapshot" in manifest.plan.skipped_events
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["unresolved_rows"]
    assert all(f"{CPI}-T3.0" in line for line in metrics["unresolved_rows"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert f"{len(metrics['unresolved_rows'])} market forecast(s)" in left_out
