"""The report's statements on where the error comes from, self-halts and late starts.

Each is read from ``metrics.json``; a run whose metrics predate a block must
still build, with that part left out.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from tests.evaluation.backtest.conftest import FullRun


def _section(text: str, heading: str) -> str:
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def _copy(full_run: FullRun, tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    shutil.copytree(full_run.run_dir, run_dir)
    return run_dir


def _edit(run_dir: Path, change: Any) -> None:
    path = run_dir / "metrics.json"
    metrics = json.loads(path.read_text())
    change(metrics)
    path.write_text(json.dumps(metrics))


def _rows(run_dir: Path, system: str) -> list[dict[str, Any]]:
    """The forecast rows one system wrote, from the run's ``forecasts.csv``."""
    import csv

    with (run_dir / "forecasts.csv").open() as f:
        found = [r for r in csv.DictReader(f) if r["system"] == system]
    return [{**r, "horizon_days": r["horizon_days"] or None} for r in found]


def test_the_error_section_shows_each_scored_systems_bands_and_largest_parts(
    full_run: FullRun, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = _copy(full_run, tmp_path)
    metrics = json.loads((run_dir / "metrics.json").read_text())
    market = metrics["systems"]["market"]["error_concentration"]
    pooled = metrics["systems"]["market"]["by_horizon"]["all"]["answered"]["sample"]["n_rows"]
    assert market["n_rows"] == pooled
    assert sum(b["n_rows"] for b in market["bands"]) == pooled
    assert len(market["top_markets"]) == min(
        3, metrics["systems"]["market"]["by_horizon"]["all"]["n_markets_in_rows"]
    )

    # The pipeline also forecast in the trading pass; those news rows are not
    # in its pooled Brier score, so they must not be in its block either.
    pipeline = metrics["systems"]["pipeline"]
    assert any(r["horizon_days"] is None for r in _rows(run_dir, "pipeline"))
    assert (
        pipeline["error_concentration"]["n_rows"]
        == pipeline["by_horizon"]["all"]["answered"]["sample"]["n_rows"]
    )

    section = _section(build_report(run_dir).read_text(), "Where the error comes from")
    for band in market["bands"]:
        assert f"| {band['name']} | {band['n_rows']} |" in section
    assert f"The event with the largest share is {market['top_event']['name']}" in section
    for m in market["top_markets"]:
        assert m["name"] in section
    assert "rests heavily on that event" in section


# A block made by hand, so the printed numbers can be checked against known
# values. Summed squared error 2.0 over 10 rows: below 0.10, 1 row, 0.02 (1.0%);
# 0.10 to 0.90, 3 rows, 0.902 (45.1%); above 0.90, 6 rows, 1.078 (53.9%).
HAND_BLOCK = {
    "n_rows": 10,
    "squared_error": 2.0,
    "bands": [
        {"name": "below 0.10", "n_rows": 1, "squared_error": 0.02, "share": 0.01},
        {"name": "0.10 to 0.90", "n_rows": 3, "squared_error": 0.902, "share": 0.451},
        {"name": "above 0.90", "n_rows": 6, "squared_error": 1.078, "share": 0.539},
    ],
    "top_event": {"name": "EV-CPI", "n_rows": 6, "squared_error": 1.078, "share": 0.539},
    "top_markets": [
        {"name": "EV-CPI-A", "n_rows": 2, "squared_error": 0.4, "share": 0.2},
        {"name": "EV-CPI-B", "n_rows": 1, "squared_error": 0.3, "share": 0.15},
        {"name": "EV-FED-C", "n_rows": 1, "squared_error": 0.1234, "share": 0.0617},
    ],
}


def test_the_error_section_prints_the_blocks_numbers(full_run: FullRun, tmp_path: Path) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = _copy(full_run, tmp_path)
    _edit(run_dir, lambda m: m["systems"]["market"].update(error_concentration=HAND_BLOCK))
    section = _section(build_report(run_dir).read_text(), "Where the error comes from")
    assert "summed squared error 2.0000 over 10 row(s)" in section
    assert "| below 0.10 | 1 | 0.0200 | 1.0% |" in section
    assert "| 0.10 to 0.90 | 3 | 0.9020 | 45.1% |" in section
    assert "| above 0.90 | 6 | 1.0780 | 53.9% |" in section
    assert "The event with the largest share is EV-CPI: 53.9% of the error, from 6 row(s)" in (
        section
    )
    assert (
        "The markets with the largest shares: EV-CPI-A 20.0% (2 row(s)); "
        "EV-CPI-B 15.0% (1 row(s)); EV-FED-C 6.2% (1 row(s))."
    ) in section


# Two self-halt blocks made by hand, different in every value, so a part of the
# report that prints the other pass's block is caught.
SCORING_HALT = {
    "first_trigger_id": "schedule:EV-CPI:7d",
    "first_episode_id": "ep-4",
    "first_started_at": "2026-08-18T12:29:00Z",
    "reason": "self-halt: drawdown 21% exceeds 20%",
    "n_from_then": 5,
    "n_halted_from_then": 4,
}
TRADING_HALT = {
    "first_trigger_id": "doc:tr-9",
    "first_episode_id": "ep-9",
    "first_started_at": "2026-08-12T14:00:00Z",
    "reason": "self-halt: fill failures on 3 of 20 orders",
    "n_from_then": 7,
    "n_halted_from_then": 6,
}
HALTS = {"scoring": SCORING_HALT, "trading": TRADING_HALT}


def _halt_report(full_run: FullRun, tmp_path: Path, halts: dict[str, Any]) -> dict[str, str]:
    """The report's parts that speak of self-halts, after ``self_halts`` is set to ``halts``."""
    from pmfa.evaluation.report import build_report

    run_dir = _copy(full_run, tmp_path)
    _edit(run_dir, lambda m: m.update(self_halts=halts))
    text = build_report(run_dir).read_text()
    exits = _section(text, "Exits by reason")
    return {
        "scoring": exits.split("Scoring pass", 1)[1].split("Trading pass", 1)[0],
        "trading": exits.split("Trading pass", 1)[1],
        "money": _section(text, "Simulated trading after fees"),
    }


def _values(halt: dict[str, Any]) -> list[str]:
    return [
        halt["first_started_at"],
        halt["first_trigger_id"],
        halt["reason"],
        f"{halt['n_halted_from_then']} of the {halt['n_from_then']}",
    ]


def test_a_self_halt_is_stated_for_each_pass(full_run: FullRun, tmp_path: Path) -> None:
    parts = _halt_report(full_run, tmp_path, HALTS)
    for name, other in (("scoring", "trading"), ("trading", "scoring")):
        for value in _values(HALTS[name]):
            assert value in parts[name], (name, value)
        for value in _values(HALTS[other]):
            assert value not in parts[name], (name, value)
    trading_part, scoring_part = parts["trading"], parts["scoring"]
    assert "no further orders" in trading_part and "no further orders" not in scoring_part
    assert "every scoring point is still forecast and scored" in scoring_part
    assert TRADING_HALT["first_started_at"] in parts["money"]
    assert SCORING_HALT["first_started_at"] not in parts["money"]


def test_a_halt_in_the_scoring_pass_only_is_stated_there_only(
    full_run: FullRun, tmp_path: Path
) -> None:
    parts = _halt_report(full_run, tmp_path, {"scoring": SCORING_HALT, "trading": None})
    for value in _values(SCORING_HALT):
        assert value in parts["scoring"], value
        assert value not in parts["trading"], value
        assert value not in parts["money"], value
    assert "ended halted" not in parts["trading"]
    assert "halted itself" not in parts["money"]


def test_a_halt_in_the_trading_pass_only_is_stated_there_only(
    full_run: FullRun, tmp_path: Path
) -> None:
    parts = _halt_report(full_run, tmp_path, {"scoring": None, "trading": TRADING_HALT})
    for value in _values(TRADING_HALT):
        assert value in parts["trading"], value
        assert value not in parts["scoring"], value
    assert TRADING_HALT["first_started_at"] in parts["money"]
    assert "ended halted" not in parts["scoring"]


def test_late_starts_are_counted_where_fills_are_described(
    full_run: FullRun, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = _copy(full_run, tmp_path)
    _edit(
        run_dir, lambda m: m.update(late_starts={"episodes": 2, "of": 5, "longest_seconds": 90.0})
    )
    priced = _section(build_report(run_dir).read_text(), "How each fill was priced")
    assert "2 of the 5 trading episode(s) started later than their trigger's time" in priced
    assert "90 seconds" in priced
    assert "candle that priced the previous order" in priced

    _edit(run_dir, lambda m: m.update(late_starts={"episodes": 0, "of": 5, "longest_seconds": 0}))
    priced = _section(build_report(run_dir).read_text(), "How each fill was priced")
    assert "None of the 5 trading episode(s) started later" in priced


def test_late_starts_are_counted_when_the_trading_pass_sent_no_order(
    full_run: FullRun, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = _copy(full_run, tmp_path)

    def no_orders(metrics: dict[str, Any]) -> None:
        metrics["fill_pricing"] = {"orders": 0, "stale_price_exits": 0}
        metrics["late_starts"] = {"episodes": 1, "of": 4, "longest_seconds": 5400.0}

    _edit(run_dir, no_orders)
    priced = _section(build_report(run_dir).read_text(), "How each fill was priced")
    assert "No order was placed" in priced
    assert "1 of the 4 trading episode(s) started later than their trigger's time" in priced
    assert "90.0 minutes" in priced


def test_a_run_without_the_new_blocks_still_reports(full_run: FullRun, tmp_path: Path) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = _copy(full_run, tmp_path)

    def strip(metrics: dict[str, Any]) -> None:
        for key in ("self_halts", "late_starts"):
            metrics.pop(key, None)
        for entry in metrics["systems"].values():
            entry.pop("error_concentration", None)

    _edit(run_dir, strip)
    text = build_report(run_dir).read_text()
    assert "## Where the error comes from" not in text
    assert "halted itself" not in text and "ended halted" not in text
    assert "started later than" not in text
    assert "## How each fill was priced" in text and "## Exits by reason" in text


def test_nothing_is_said_about_halts_when_no_episode_halted(
    full_run: FullRun, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = _copy(full_run, tmp_path)
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert all("halted" not in t["exits"] for t in metrics["episodes"].values())
    assert metrics["self_halts"] == {"scoring": None, "trading": None}
    text = build_report(run_dir).read_text()
    assert "halted itself" not in text and "ended halted" not in text
