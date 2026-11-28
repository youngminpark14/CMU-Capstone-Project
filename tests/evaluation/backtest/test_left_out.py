"""The report names everything the window held that was not scored, and counts what was."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

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
    assert "1 release(s) in the window had no event" in _section(text, "Limitations")

    _set_dropped(data_config, {})
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert "the exchange listed no KXCPIYOY event for this release" in left_out
    assert "the builder dropped no KXCPIYOY event" in left_out

    _set_dropped(data_config, {f"{CPI}-26SEPX": "listed with no markets"})
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert f"{CPI}-26SEPX: listed with no markets" in left_out
    # No close time on record: the event can be neither matched to the release nor ruled out.
    assert f"{CPI}-26SEPX has no close time on record" in left_out
    assert "listed no KXCPIYOY event for this release" not in left_out


def _far_and_near(close: str) -> dict[str, str]:
    window = "the window 2026-07-01T00:00:00+00:00 to 2026-11-25T00:00:00+00:00"
    return {
        f"{CPI}-26DEC": f"closes 2027-01-13T13:29:00+00:00, after the end of {window}",
        f"{CPI}-26NOVX": f"closes {close}, after the end of {window}",
        f"{FED}-27JAN": f"closes 2026-08-30T18:59:00+00:00, after the end of {window}",
    }


def test_a_release_whose_series_dropped_only_far_events_had_no_event_listed(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    _add_release(data_config)
    # Every KXCPIYOY event the builder dropped closes weeks from the 2026-08-30 release; the
    # Fed event closing that day is of another series and says nothing about this one.
    _set_dropped(data_config, _far_and_near("2026-12-10T13:29:00+00:00"))
    run_dir = tmp_path / "run"
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert "the exchange listed no KXCPIYOY event for this release" in left_out
    assert "none of the 2 KXCPIYOY event(s) the builder dropped closes within two days" in left_out
    assert "may be" not in left_out and "may belong" not in left_out


def test_a_dropped_event_closing_near_a_release_is_named_as_possibly_its_event(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    _add_release(data_config)
    _set_dropped(data_config, _far_and_near("2026-08-30T12:29:00+00:00"))
    run_dir = tmp_path / "run"
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert f"{CPI}-26NOVX, which closes within two days of the release" in left_out
    assert f"{CPI}-26DEC," not in left_out.split("Events the exchange listed")[0]
    assert "listed no KXCPIYOY event for this release" not in left_out


def test_a_dropped_event_closing_a_day_and_a_half_after_a_release_is_named(
    data_config: Config, tmp_path: Path
) -> None:
    """The report matches a dropped event to a release by the schedule's own rule, two days.

    The CPI-26NOVX close is 36 hours after the 2026-08-30 12:30 release: within
    the schedule's match, so the report must not say the exchange listed no event.
    """
    from pmfa.evaluation.report import build_report

    _add_release(data_config)
    _set_dropped(data_config, _far_and_near("2026-09-01T00:30:00+00:00"))
    run_dir = tmp_path / "run"
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    left_out = _section(build_report(run_dir).read_text(), "Left out of scoring")
    assert f"{CPI}-26NOVX, which closes within two days of the release" in left_out
    assert "listed no KXCPIYOY event for this release" not in left_out


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


RELEASE = (
    "cpi_release:2026-07-extra (Consumer Price Index, an extra release, "
    "2026-08-30T12:30:00+00:00): no KXCPIYOY event in the snapshot"
)


def test_spans_are_said_in_words() -> None:
    from datetime import timedelta

    from pmfa.evaluation.report import _span

    assert _span(timedelta(days=2)) == "two days"
    assert _span(timedelta(days=1)) == "a day"
    assert _span(timedelta(hours=36)) == "36 hours"
    assert _span(timedelta(minutes=90)) == "90 minutes"


def test_the_release_rule_and_its_wording_follow_the_schedules_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Change the match to 36 hours: a close 30 hours away is named, one 40 hours away is not."""
    from datetime import timedelta

    from pmfa.evaluation import report

    monkeypatch.setattr(report, "RELEASE_MATCH", timedelta(hours=36))
    near = {f"{CPI}-26NOVX": "closes 2026-08-31T18:30:00+00:00, after the window"}
    far = {f"{CPI}-26NOVX": "closes 2026-09-01T04:30:00+00:00, after the window"}
    named = report._release_verdict(RELEASE, near)
    assert f"{CPI}-26NOVX, which closes within 36 hours of the release" in named
    ruled_out = report._release_verdict(RELEASE, far)
    assert "the exchange listed no KXCPIYOY event for this release" in ruled_out
    assert "closes within 36 hours of it" in ruled_out


def test_a_dropped_event_closing_exactly_the_match_after_a_release_is_named() -> None:
    """The schedule's rule is inclusive, so a close exactly two days after the release matches."""
    from pmfa.evaluation import report
    from pmfa.evaluation.schedule import RELEASE_MATCH

    released = datetime(2026, 8, 30, 12, 30, tzinfo=UTC)
    edge = {f"{CPI}-26NOVX": f"closes {(released + RELEASE_MATCH).isoformat()}, after the window"}
    named = report._release_verdict(RELEASE, edge)
    assert f"{CPI}-26NOVX, which closes within two days of the release" in named
    assert "listed no KXCPIYOY event for this release" not in named
