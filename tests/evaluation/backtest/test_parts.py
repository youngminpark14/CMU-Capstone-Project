"""The parts of the backtest on their own: schedule, resolver, baselines conversion, report, publishing."""

from __future__ import annotations

import csv
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


def test_a_backtest_never_asks_for_the_server_side_refusal_fallback() -> None:
    """Only the assigned models, whose knowledge cutoffs the run checked, may answer."""
    from pmfa.config import default_config
    from pmfa.models.backend import AnthropicBackend
    from pmfa.world.backtest import backtest_config

    config = default_config()
    assert AnthropicBackend.from_config(config).uses_fallback("claude-fable-5-2")
    forced = backtest_config(config)
    assert forced.models.refusal_fallback == "off"
    backend = AnthropicBackend.from_config(forced)
    assert not any(backend.uses_fallback(slot.model) for slot in forced.models.slots.values())
    # Nothing else about the models changes.
    assert forced.models.model_copy(update={"refusal_fallback": "default"}) == config.models


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
            OptionProbability(outcome_id="hold", probability=0.5),
            OptionProbability(outcome_id="cut_25", probability=0.51),
        ],
        quantiles=None,
        rationale="",
    )
    dist, problem = outcome_distribution(good, space)
    assert problem == "" and dist is not None
    assert dist["hold"] == pytest.approx(0.5 / 1.01) and dist["hike_25"] == 0.0
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
    target = publish_run(
        full_run.run_dir,
        "2026-08-31-test",
        root=tmp_path / "evaluation" / "runs",
        allow_supplied=True,
    )
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


@pytest.mark.parametrize(
    ("named", "why"),
    [
        ({"hold": 0.6}, "sum to 0.600, not 1"),  # a partial answer is not rescaled to certainty
        ({"hold": 2.0, "cut_25": 2.0}, "sum to 4.000, not 1"),
        ({"hold": float("nan"), "cut_25": 1.0}, "not a finite number"),
    ],
)
def test_a_baseline_answer_that_does_not_sum_to_one_abstains(
    data_config: Config, named: dict[str, float], why: str
) -> None:
    sc = data_config.follow.for_ticker("KXFEDDECISION")
    assert sc is not None
    space = build_outcome_space(fed_event(), sc)
    answer = BaselineForecast(
        option_probabilities=[
            OptionProbability(outcome_id=k, probability=v) for k, v in named.items()
        ],
        quantiles=None,
        rationale="",
    )
    dist, problem = outcome_distribution(answer, space)
    assert dist is None and why in problem


def test_an_out_of_range_probability_is_clamped_with_a_note() -> None:
    from pmfa.evaluation.baselines import SystemForecast, forecast_rows
    from pmfa.records.market import Side

    forecast = SystemForecast(
        system="pipeline",
        event_ticker=FED,
        as_of=datetime(2026, 8, 13, tzinfo=UTC),
        market_probabilities={f"{FED}-H0": 1.2, f"{FED}-C25": float("nan"), f"{FED}-H25": 0.1},
    )
    rows = forecast_rows(
        forecast,
        run_id="r",
        category="fed_decision",
        prices={f"{FED}-H0": 0.6, f"{FED}-C25": 0.3, f"{FED}-H25": 0.1},
        results={f"{FED}-H0": Side.YES, f"{FED}-C25": Side.NO, f"{FED}-H25": Side.NO},
        horizon_days=7,
    )
    by = {row.market_ticker: (row, note) for row, note in rows}
    assert by[f"{FED}-H0"][0].probability == 1.0
    assert "clamped" in by[f"{FED}-H0"][1].note
    assert by[f"{FED}-C25"][0].probability is None
    assert "not a finite number" in by[f"{FED}-C25"][1].note
    assert by[f"{FED}-H25"][1].note == ""


def test_a_run_answered_by_supplied_models_is_not_published_as_a_result(
    full_run: FullRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted answers (a test, or a rehearsal) are not results, so publishing is refused."""
    from typer.testing import CliRunner

    from pmfa.cli.app import app
    from pmfa.evaluation.report import build_report, publish_run

    build_report(full_run.run_dir)
    assert full_run.manifest.models_source == "supplied"
    with pytest.raises(ValueError, match="not a result"):
        publish_run(full_run.run_dir, "x", root=tmp_path / "runs")
    assert not (tmp_path / "runs").exists()  # refused before anything was written
    # The command line has no way to override the refusal.
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["report", str(full_run.run_dir), "--publish", "x"])
    assert result.exit_code == 1
    assert not (tmp_path / "evaluation").exists()


def test_published_outputs_list_exactly_what_was_copied(full_run: FullRun, tmp_path: Path) -> None:
    from pmfa.evaluation.manifest import read_manifest
    from pmfa.evaluation.report import build_report, publish_run

    build_report(full_run.run_dir)
    target = publish_run(full_run.run_dir, "outputs", root=tmp_path / "runs", allow_supplied=True)
    outputs = read_manifest(target).outputs
    for entry in outputs:
        assert (target / entry).exists(), entry
    copied = {p.relative_to(target).as_posix() for p in target.iterdir()}
    copied |= {f"figures/{p.name}" for p in (target / "figures").iterdir()}
    copied |= {f"traces/{p.name}" for p in (target / "traces").iterdir()}
    assert set(outputs) == copied - {"figures", "traces"}
    assert "trades.csv" not in outputs and "episodes.jsonl" not in outputs


def test_a_scores_only_publication_leaves_out_the_rows_and_the_traces_and_says_so(
    full_run: FullRun, tmp_path: Path
) -> None:
    """The per-forecast table and the traces hold exchange prices; scores only leaves them home."""
    from pmfa.evaluation.manifest import read_manifest
    from pmfa.evaluation.report import SCORES_ONLY_NOTE, build_report, publish_run

    build_report(full_run.run_dir)
    target = publish_run(
        full_run.run_dir, "scores", root=tmp_path / "runs", allow_supplied=True, scores_only=True
    )
    published = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert {p for p in published if "/" not in p} == {"manifest.json", "metrics.json", "report.md"}
    assert {p.split("/")[0] for p in published if "/" in p} == {"figures"}
    manifest = read_manifest(target)
    assert set(manifest.outputs) == published
    assert SCORES_ONLY_NOTE in manifest.notes
    assert (full_run.run_dir / "forecasts.csv").exists()  # the rows stay in the run directory

    # The published report says what was left out and points at no file that is not there.
    report = (target / "report.md").read_text()
    flat = " ".join(report.split())
    assert SCORES_ONLY_NOTE in flat
    assert "the per-forecast table is `forecasts.csv`" not in flat
    files = " ".join(report.split("## Files", 1)[1].split())
    assert "Not published, kept in the local run directory: `forecasts.csv`" in files
    assert "- `forecasts.csv`:" not in report and "- `traces/`:" not in report
    assert "replays and matches" in files  # the replay check of each saved trace is still told
    # Everything else is the run's own report, which still describes the local files.
    local = (full_run.run_dir / "report.md").read_text()
    assert "the per-forecast table is `forecasts.csv`" in local and "- `traces/`:" in local
    assert SCORES_ONLY_NOTE not in local
    headings = [line for line in local.splitlines() if line.startswith("## ")]
    assert headings == [line for line in report.splitlines() if line.startswith("## ")]

    def section(text: str, heading: str) -> str:
        return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]

    for heading in (
        "What was run and what was not",
        "Sample sizes",
        "Brier score and Brier skill against the market",
        "Exits by reason",
        "How look-ahead was prevented",
    ):
        assert section(report, heading) == section(local, heading), heading


def test_a_scores_only_publication_leaves_out_what_gives_fill_or_market_prices(
    full_run: FullRun, tmp_path: Path
) -> None:
    """Cash at each fill, P&L by market, and one market's squared error each give its prices."""
    import json

    from pmfa.evaluation.report import build_report, publish_run

    build_report(full_run.run_dir)
    local = json.loads((full_run.run_dir / "metrics.json").read_text())
    assert local["pnl"]["equity_curve"] and local["pnl"]["summary"]["by_market"]
    assert any("error_concentration" in s for s in local["systems"].values())
    target = publish_run(
        full_run.run_dir, "scores", root=tmp_path / "runs", allow_supplied=True, scores_only=True
    )
    text = (target / "metrics.json").read_text()
    for key in ("equity_curve", "by_market", "by_event", "top_markets", "top_event"):
        assert f'"{key}"' not in text, key
    published = json.loads(text)
    assert published["systems"].keys() == local["systems"].keys()
    assert published["pnl"]["summary"]["n_settled"] == local["pnl"]["summary"]["n_settled"]
    report = (target / "report.md").read_text()
    assert "The event with the largest share" not in report
    error_section = " ".join(report.split("## Where the error comes from", 1)[1].split())
    assert "split by the forecast probability." in error_section
    assert "by event and by market is left out of a scores-only publication" in error_section

    # A profit and its fees have one solution in whole contracts and cent prices, and an
    # interval over two events is twice each event's result. So no dollar result is told.
    pnl, kept = published["pnl"], local["pnl"]
    assert kept["summary"]["n_trades"] == pnl["summary"]["n_trades"] == 2
    assert pnl["withheld"] == {"why": "scores only", "result": "a gain"}
    assert float(kept["summary"]["total"]) > 0
    for block in (pnl["summary"], *pnl["by_routing"].values()):
        assert block["total"] is None and block["fees"] is None and block["open_cost"] is None
    assert pnl["max_drawdown"] == {"fraction": None, "dollars": None}
    assert [pnl["interval"][k] for k in ("estimate", "lower", "upper")] == [None, None, None]
    dollars = {
        f"{float(kept['summary'][k]):,.2f}" for k in ("total", "fees") if float(kept["summary"][k])
    }
    dollars |= {f"{float(kept['interval'][k]):,.2f}" for k in ("lower", "upper")}
    assert dollars and not any(f"${value}" in report for value in dollars)
    trading = " ".join(report.split("## Simulated trading after fees", 1)[1].split())
    assert "| Realized P&L after fees | a gain (dollars withheld) |" in trading
    assert "The simulation made 2 fill(s), and its realized result after fees was a gain" in trading
    assert "fewer than ten" not in report and "fewer than 10" not in report

    # The market's forecasts are exchange prices, so a bin of its table with fewer than five
    # would show single prices. The other systems' bins are their own forecasts and stay. The
    # table that scores abstentions as the market keeps only its count, for every system.
    small = large = 0
    for system, tables in local["reliability"].items():
        mine_all, theirs_all = tables["answered"], published["reliability"][system]["answered"]
        withheld = 0
        for mine, theirs in zip(mine_all["bins"], theirs_all["bins"], strict=True):
            if system == "market" and 0 < mine["count"] < 5:
                small += 1
                withheld += 1
                assert theirs["mean_forecast"] is None, (system, mine)
            else:
                large += bool(mine["count"])
                assert theirs["mean_forecast"] == mine["mean_forecast"]
            assert theirs["count"] == mine["count"]
            assert theirs["observed_frequency"] == mine["observed_frequency"]
        assert theirs_all["ece"] == (None if withheld == 1 else mine_all["ece"]), system
        other = published["reliability"][system]["abstain_as_market"]
        assert other == {"n_rows": tables["abstain_as_market"]["n_rows"]}, system
    assert small and large
    reliability_section = " ".join(
        report.split("## Reliability", 1)[1].split("\n## ", 1)[0].split()
    )
    assert "| withheld |" in reliability_section
    left_out = "the table and its calibration error are left out of a scores-only publication"
    assert reliability_section.count(left_out) == len(local["reliability"])
    assert "that table is in `metrics.json`" in " ".join(local_report(full_run).split())
    for ticker in local["pnl"]["summary"]["by_market"]:
        assert ticker not in report
    assert json.loads((full_run.run_dir / "metrics.json").read_text()) == local  # left as it was


def local_report(full_run: FullRun) -> str:
    return (full_run.run_dir / "report.md").read_text()


def _pnl_metrics(fills: int, total: str = "12.34") -> dict:
    def block() -> dict:
        return {
            "total": total,
            "fees": "1.20",
            "open_cost": "0.50",
            "n_trades": fills,
            "n_settled": fills,
            "by_market": {"M": total},
            "by_event": {"E": total},
        }

    return {
        "systems": {},
        "reliability": {},
        "pnl": {
            "bankroll": "1000",
            "summary": block(),
            "by_routing": {"auto": {**block(), "n_fills": fills}},
            "interval": {"estimate": total, "lower": "1.00", "upper": "20.00", "n_clusters": 2},
            "max_drawdown": {"fraction": "0.01", "dollars": "10.00"},
            "equity_curve": [{"at": "2026-08-10T14:02:00Z", "cash": "993.73"}],
        },
    }


@pytest.mark.parametrize("fills", [1, 10, 250])
def test_scores_only_withholds_every_dollar_result_whatever_the_number_of_fills(
    fills: int,
) -> None:
    """A tier or an event can hold one fill however many there are, so no count is enough."""
    from pmfa.evaluation.report import _scores_only_metrics

    pnl = _scores_only_metrics(_pnl_metrics(fills))["pnl"]
    assert pnl["withheld"] == {"why": "scores only", "result": "a gain"}
    for block in (pnl["summary"], pnl["by_routing"]["auto"]):
        assert [block[k] for k in ("total", "fees", "open_cost")] == [None, None, None]
        assert block["n_trades"] == block["n_settled"] == fills  # the counts stay
        assert "by_market" not in block and "by_event" not in block
    assert pnl["by_routing"]["auto"]["n_fills"] == fills
    assert pnl["max_drawdown"] == {"fraction": None, "dollars": None}
    assert pnl["interval"] == {"estimate": None, "lower": None, "upper": None, "n_clusters": 2}
    assert "equity_curve" not in pnl
    assert _scores_only_metrics(_pnl_metrics(3, "-0.40"))["pnl"]["withheld"]["result"] == "a loss"
    even = _scores_only_metrics(_pnl_metrics(3, "0"))["pnl"]["withheld"]
    assert even["result"] == "no gain or loss"


@pytest.mark.parametrize(("small_bins", "ece_kept"), [(0, True), (1, False), (2, True)])
def test_the_calibration_error_is_withheld_when_it_would_give_back_the_one_withheld_mean(
    small_bins: int, ece_kept: bool
) -> None:
    """The error is a count-weighted sum over the bins, so with one unknown mean it gives it."""
    from pmfa.evaluation.report import _scores_only_metrics

    def bin_(count: int) -> dict:
        return {"count": count, "mean_forecast": 0.85, "observed_frequency": 1.0}

    bins = [bin_(3) for _ in range(small_bins)] + [bin_(12), bin_(0)]
    tables = {
        "answered": {"bins": bins, "ece": 0.137},
        "abstain_as_market": {"n_rows": 15 + small_bins, "ece": 0.136, "bins": bins},
    }
    published = _scores_only_metrics({"reliability": {"market": tables, "pipeline": tables}})
    out = published["reliability"]["market"]
    assert [b["mean_forecast"] for b in out["answered"]["bins"]] == [None] * small_bins + [
        0.85,
        0.85,
    ]
    assert out["answered"]["ece"] == (0.137 if ece_kept else None)
    assert out["abstain_as_market"] == {"n_rows": 15 + small_bins}
    # Another system's bins hold its own forecasts, not prices: its answered table stays whole.
    other = published["reliability"]["pipeline"]
    assert other["answered"] == tables["answered"]
    assert other["abstain_as_market"] == {"n_rows": 15 + small_bins}


def test_scores_only_with_no_fill_has_no_dollar_result_to_withhold() -> None:
    from pmfa.evaluation.report import _scores_only_metrics

    pnl = _scores_only_metrics(_pnl_metrics(0, "0"))["pnl"]
    assert "withheld" not in pnl and pnl["summary"]["total"] == "0"


def test_a_self_halt_reason_keeps_only_the_name_of_a_stop_that_states_dollars() -> None:
    """The daily loss stop and the drawdown state the day's starting and the peak equity."""
    from pmfa.evaluation.report import _scores_only_metrics

    loss = (
        "daily loss stop: equity fell 0.01000 or more (0.00001 of the day's starting equity "
        "1000) below that start today; day P&L now -0.1100"
    )
    drawdown = "drawdown: 0.0001 below the peak 1000.37, limit 0.00001"
    others = [
        "fill failures: 2 of the last 10 orders did not fill",
        "calibration: cpi error above 0.1 for 2 weeks (now 0.102)",
        "source changed: fed:minutes behind an open position has new content",
    ]
    cut = " (its dollar figures are left out of a scores-only publication)"
    cases = {
        f"self-halt: {loss}": f"self-halt: daily loss stop{cut}",
        f"self-halt: {loss}; {drawdown}; {others[0]}": (
            f"self-halt: daily loss stop{cut}; drawdown{cut}; {others[0]}"
        ),
        f"the halt flag is set: {drawdown}; {loss}": (
            f"the halt flag is set: drawdown{cut}; daily loss stop{cut}"
        ),
        "self-halt: " + "; ".join(others): "self-halt: " + "; ".join(others),
        "gate: halt": "gate: halt",
    }
    for reason, expected in cases.items():
        metrics = {"self_halts": {"trading": {"reason": reason}, "scoring": None}}
        assert _scores_only_metrics(metrics)["self_halts"]["trading"]["reason"] == expected


def _rows_without(rows: list, system: str | None, horizon: int | None, *, drop: bool) -> list:
    """``rows`` with ``system``'s forecasts for the fixture's Fed event at ``horizon`` taken out.

    ``drop`` removes the rows (the point was not run); otherwise they become abstentions,
    as after a failed call. ``horizon`` None means the news-triggered forecasts.
    """
    out = []
    for r in rows:
        hit = (
            r.system == system
            and r.horizon_days == horizon
            and r.event_ticker == "KXFEDDECISION-26AUG"
        )
        if not hit:
            out.append(r)
        elif not drop:
            out.append(r.model_copy(update={"probability": None}))
    return out


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        # One failed baseline call loses one event's markets at one as-of time.
        (
            "prompt_only abstains on one event at 3 days",
            "prompt_only did not answer 3 forecast(s) in the 3-day horizon that the run holds",
        ),
        ("the market lacks one event at 3 days", "market did not answer 3 forecast(s)"),
        (
            "no market system; prompt_only lacks one event at 7 days",
            "prompt_only did not answer 3 forecast(s) in the 7-day horizon",
        ),
        (
            "pipeline abstains on one event's news-triggered forecasts",
            "pipeline did not answer 3 forecast(s) in the news-triggered forecasts",
        ),
        ("nothing lacking", ""),
        ("prompt_only has no forecast at all at 1 day", ""),
    ],
)
def test_a_run_in_which_a_system_lacks_a_few_forecasts_is_refused_as_scores_only(
    full_run: FullRun, change: str, refused: str
) -> None:
    """The difference between two systems' market scores gives the prices on what one lacks."""
    from pmfa.evaluation.backtest import read_forecasts
    from pmfa.evaluation.report import scores_only_refusal

    rows = [row for row, _ in read_forecasts(full_run.run_dir / "forecasts.csv")]
    if change == "prompt_only abstains on one event at 3 days":
        rows = _rows_without(rows, "prompt_only", 3, drop=False)
    elif change == "the market lacks one event at 3 days":
        rows = _rows_without(rows, "market", 3, drop=True)
    elif change.startswith("no market system"):
        rows = _rows_without([r for r in rows if r.system != "market"], "prompt_only", 7, drop=True)
    elif change.startswith("pipeline abstains"):
        rows = _rows_without(rows, "pipeline", None, drop=False)
    elif change == "prompt_only has no forecast at all at 1 day":
        rows = [r for r in rows if not (r.system == "prompt_only" and r.horizon_days == 1)]
    reason = scores_only_refusal(rows)
    assert (refused in reason and reason) if refused else reason == "", (change, reason)
    if refused:
        assert reason.startswith("this run cannot be published as scores only: ")
        assert "fewer than 10" in reason
        assert "--scores-only" not in reason and "per-forecast table" not in reason


def _ladder_rows(full_run: FullRun, unanswered: dict[str, range]) -> list:
    """Three systems on one 30-market event at one horizon; each leaves the given markets unanswered."""
    from pmfa.evaluation.backtest import read_forecasts

    template = next(row for row, _ in read_forecasts(full_run.run_dir / "forecasts.csv"))
    rows = []
    for system in ("market", "prompt_only", "pipeline"):
        for i in range(30):
            rows.append(
                template.model_copy(
                    update={
                        "system": system,
                        "horizon_days": 3,
                        "event_ticker": "KXCPIYOY-26AUG",
                        "market_ticker": f"KXCPIYOY-26AUG-T{i}",
                        "probability": None if i in unanswered.get(system, ()) else 0.5,
                    }
                )
            )
    return rows


@pytest.mark.parametrize(
    ("unanswered", "refused"),
    [
        # A whole inflation event the pipeline abstained on: one more aggregate, published.
        ({"pipeline": range(21)}, ""),
        ({"pipeline": range(10)}, ""),
        ({"pipeline": range(9)}, "pipeline did not answer 9 forecast(s)"),
        # Each lacks enough against the run, but one answered a single forecast the other did not.
        (
            {"pipeline": range(11), "prompt_only": range(10)},
            "pipeline did not answer 1 forecast(s) in the 3-day horizon that prompt_only answered",
        ),
        ({"pipeline": range(21), "prompt_only": range(21)}, ""),
    ],
)
def test_many_unanswered_forecasts_are_published_and_a_few_between_two_systems_are_not(
    full_run: FullRun, unanswered: dict[str, range], refused: str
) -> None:
    from pmfa.evaluation.report import scores_only_refusal

    reason = scores_only_refusal(_ladder_rows(full_run, unanswered))
    assert (refused in reason and reason) if refused else reason == "", reason


def test_a_refused_scores_only_publication_writes_and_deletes_nothing(
    full_run: FullRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from typer.testing import CliRunner

    from pmfa.cli.app import app
    from pmfa.evaluation.manifest import read_manifest, write_manifest
    from pmfa.evaluation.report import build_report, publish_run

    run_dir = tmp_path / "run"
    shutil.copytree(full_run.run_dir, run_dir)
    # A run answered by the gateway, in which prompt_only's call failed for one event at 3 days.
    write_manifest(read_manifest(run_dir).model_copy(update={"models_source": "gateway"}), run_dir)
    table = run_dir / "forecasts.csv"
    with table.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields, raw = reader.fieldnames, list(reader)
    for r in raw:
        if (r["system"], r["horizon_days"], r["event_ticker"]) == (
            "prompt_only",
            "3",
            "KXFEDDECISION-26AUG",
        ):
            r["probability"] = ""
    with table.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(raw)
    build_report(run_dir)
    earlier = tmp_path / "evaluation" / "runs" / "x"
    earlier.mkdir(parents=True)
    (earlier / "report.md").write_text("an earlier publication")

    with pytest.raises(
        ValueError, match=r"prompt_only did not answer 3 forecast\(s\) in the 3-day"
    ):
        publish_run(run_dir, "x", root=tmp_path / "evaluation" / "runs", scores_only=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["report", str(run_dir), "--publish", "x", "--scores-only"])
    assert result.exit_code == 1
    printed = " ".join(result.output.split())
    assert "not published: this run cannot be published as scores only: prompt_only" in printed
    assert "Keep it unpublished" in printed
    assert "without --scores-only" not in printed and "per-forecast table" not in printed
    assert [p.name for p in earlier.iterdir()] == ["report.md"]  # the earlier one is left as it was
    assert (earlier / "report.md").read_text() == "an earlier publication"
