"""The report builder: ``report.md`` and ``figures/`` from a run directory alone, and publishing.

``build_report(run_dir)`` reads ``manifest.json`` and ``metrics.json`` (both
written by the runner) and writes a Markdown report with three figures. Every
number in it comes from those files; a system that did not run says "not run"
with the reason, and an interval the bootstrap could not define says "not
defined". Nothing is estimated or filled in here.

``publish_run(run_dir, name)`` copies the committed artifact set of a reported
run to ``evaluation/runs/NAME/``: the manifest, the per-forecast table, the
metrics, the report with its figures, and the saved traces.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pmfa.evaluation.baselines import MARKET_PRICE_RULE, PIPELINE, SYSTEMS
from pmfa.evaluation.manifest import MANIFEST_FILE, read_manifest, write_manifest
from pmfa.evaluation.schedule import RELEASE_MATCH

REPORT_FILE = "report.md"
FIGURES_DIR = "figures"
FIGURES = ("brier_by_horizon.png", "reliability.png", "equity_curve.png")
COMMITTED = ("manifest.json", "forecasts.csv", "metrics.json", REPORT_FILE)
"""Files of a published run; ``figures/`` and ``traces/`` are copied whole beside them."""
PUBLISH_ROOT = Path("evaluation") / "runs"

# Reference categorical hues, in a fixed order per system (never cycled), and text inks.
SYSTEM_COLORS = {
    "market": "#257de0",
    "prompt_only": "#f47230",
    "single_pass": "#00b894",
    "single_pass_similarity": "#f2a600",
    "pipeline": "#ee7fac",
}
TEXT = "#191919"
TEXT_2 = "#666666"
GRID = "#e5e5e5"

SYSTEM_LABELS = {
    "market": "Market (YES mid, else last trade within the quote)",
    "prompt_only": "Prompt only",
    "single_pass": "Single pass (recency and origin ranking)",
    "single_pass_similarity": "Single pass (similarity ranking)",
    "pipeline": "Full pipeline",
}


# --------------------------------------------------------------------------- formatting


def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "not defined"
    return f"{float(value):.{places}f}"


def _interval(block: dict[str, Any] | None, places: int = 4) -> str:
    if not block:
        return "not defined"
    est = block.get("estimate")
    if est is None:
        return "not defined"
    lo, hi = block.get("lower"), block.get("upper")
    if lo is None or hi is None:
        return f"{_num(est, places)} (interval not defined)"
    return f"{_num(est, places)} [{_num(lo, places)}, {_num(hi, places)}]"


def _money(value: Any) -> str:
    if value is None:
        return "not defined"
    return f"${float(value):,.2f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def _counts_table(counts: dict[str, int], what: str) -> str:
    if not counts:
        return f"No {what}."
    return _table([what.capitalize(), "Count"], [[k, str(v)] for k, v in counts.items()])


def _ran(status: str) -> bool:
    return status == "run" or status.startswith("partial")


def _trading_not_run(manifest: Any) -> str:
    """Why the trading simulation did not run, or ``""`` when it ran at least one trigger."""
    pipe = manifest.systems.get(PIPELINE, "not requested")
    if not _ran(pipe):
        return f"the pipeline was {pipe}"
    if not manifest.plan.trade:
        return "the run was made with --no-trade"
    if manifest.plan.triggers == 0:
        return "the window holds no trading trigger"
    if not manifest.progress.triggers_done:
        return "the run stopped before the trading pass ran its first trigger"
    return ""


def _label(manifest: Any, system: str) -> str:
    """The system's name, with how many points it scored when a partial run cut it short."""
    counts = _short_points(manifest, system)
    return f"{SYSTEM_LABELS[system]} ({counts})" if counts else SYSTEM_LABELS[system]


def _short_points(manifest: Any, system: str) -> str:
    """``k of n points`` when a partial run scored the system on fewer points than planned."""
    status = manifest.systems.get(system, "")
    if not status.startswith("partial: "):
        return ""
    counts = status.removeprefix("partial: ").split(",", 1)[0].split(" (", 1)[0]
    parts = counts.split()
    if len(parts) >= 3 and parts[1] == "of" and parts[0] != parts[2]:
        return counts
    return ""


# --------------------------------------------------------------------------- figures


def _axes(ax: Any) -> None:
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(TEXT_2)
    ax.tick_params(colors=TEXT_2, labelsize=9)
    ax.title.set_color(TEXT)
    ax.xaxis.label.set_color(TEXT_2)
    ax.yaxis.label.set_color(TEXT_2)


def _empty(ax: Any, message: str) -> None:
    ax.text(0.5, 0.5, message, ha="center", va="center", color=TEXT_2, fontsize=11, wrap=True)
    ax.set_xticks([])
    ax.set_yticks([])
    for side in ax.spines.values():
        side.set_visible(False)


def _figures(folder: Path, manifest: Any, metrics: dict[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    folder.mkdir(parents=True, exist_ok=True)
    horizons = [
        str(h) for h in sorted(manifest.config["evaluation"]["horizons_days"], reverse=True)
    ]
    ran = [s for s in SYSTEMS if s in metrics["systems"] and metrics["systems"][s]["by_horizon"]]

    # 1. Brier score by horizon, answered forecasts, with event-clustered intervals.
    fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=120)
    if ran:
        width = 0.8 / len(ran)
        for i, system in enumerate(ran):
            xs, ys, lo, hi = [], [], [], []
            for j, h in enumerate(horizons):
                block = metrics["systems"][system]["by_horizon"].get(h)
                if not block:
                    continue
                brier = block["answered"]["brier"]
                if brier["estimate"] is None:
                    continue
                xs.append(j + (i - (len(ran) - 1) / 2) * width)
                ys.append(brier["estimate"])
                lo.append(
                    brier["estimate"]
                    - (brier["lower"] if brier["lower"] is not None else brier["estimate"])
                )
                hi.append(
                    (brier["upper"] if brier["upper"] is not None else brier["estimate"])
                    - brier["estimate"]
                )
            ax.bar(
                xs,
                ys,
                width=width * 0.9,
                color=SYSTEM_COLORS[system],
                label=SYSTEM_LABELS[system],
                edgecolor="white",
                linewidth=1,
            )
            ax.errorbar(xs, ys, yerr=[lo, hi], fmt="none", ecolor=TEXT_2, elinewidth=1, capsize=3)
        ax.set_xticks(range(len(horizons)), [f"{h} day(s) before close" for h in horizons])
        ax.set_ylabel("Brier score (lower is better)")
        ax.set_title("Brier score by horizon, 95% interval resampling events", fontsize=11)
        ax.legend(frameon=False, fontsize=8, labelcolor=TEXT)
        _axes(ax)
    else:
        _empty(ax, "No system produced scored forecasts in this run.")
    fig.tight_layout()
    fig.savefig(folder / FIGURES[0])
    plt.close(fig)

    # 2. Reliability diagram, answered forecasts.
    fig, ax = plt.subplots(figsize=(5.5, 5.2), dpi=120)
    if ran:
        ax.plot(
            [0, 1], [0, 1], color=TEXT_2, linewidth=1, linestyle="--", label="Perfect calibration"
        )
        for system in ran:
            table = metrics["reliability"].get(system, {}).get("answered")
            if not table:
                continue
            pts = [
                (b["mean_forecast"], b["observed_frequency"], b["count"])
                for b in table["bins"]
                if b["count"]
            ]
            if not pts:
                continue
            ax.plot(
                [p[0] for p in pts],
                [p[1] for p in pts],
                color=SYSTEM_COLORS[system],
                linewidth=2,
                marker="o",
                markersize=6,
                markeredgecolor="white",
                label=SYSTEM_LABELS[system],
            )
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel("Mean forecast in bin")
        ax.set_ylabel("Share that resolved YES")
        ax.set_title("Reliability (all horizons pooled)", fontsize=11)
        ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="upper left")
        _axes(ax)
    else:
        _empty(ax, "No system produced scored forecasts in this run.")
    fig.tight_layout()
    fig.savefig(folder / FIGURES[1])
    plt.close(fig)

    # 3. Equity curve of the trading simulation.
    fig, ax = plt.subplots(figsize=(7.5, 3.8), dpi=120)
    curve = metrics.get("pnl", {}).get("equity_curve") or []
    if curve:
        from datetime import datetime

        bankroll = float(metrics["pnl"]["bankroll"])
        times = [datetime.fromisoformat(p["at"]) for p in curve]
        equity = [float(p["equity"]) for p in curve]
        ax.step(
            [times[0], *times],
            [bankroll, *equity],
            where="post",
            color=SYSTEM_COLORS[PIPELINE],
            linewidth=2,
            label="Equity (open positions at cost)",
        )
        ax.axhline(bankroll, color=TEXT_2, linewidth=1, linestyle="--", label="Not trading")
        ax.set_ylabel("Dollars")
        ax.set_title("Simulated equity after fees, news-triggered trading", fontsize=11)
        ax.legend(frameon=False, fontsize=8, labelcolor=TEXT)
        fig.autofmt_xdate()
        _axes(ax)
    else:
        why = _trading_not_run(manifest)
        _empty(
            ax,
            f"The trading simulation did not run: {why}."
            if why
            else "The trading simulation ran and made no fill.",
        )
    fig.tight_layout()
    fig.savefig(folder / FIGURES[2])
    plt.close(fig)


# --------------------------------------------------------------------------- the report


def _status_of(manifest: Any, system: str) -> str:
    return manifest.systems.get(system, "not requested")


def build_report(run_dir: str | Path) -> Path:
    """Write ``report.md`` and ``figures/`` for the run in ``run_dir``. Returns the report's path."""
    run_dir = Path(run_dir)
    manifest = read_manifest(run_dir)
    metrics_path = run_dir / "metrics.json"
    metrics: dict[str, Any] = (
        json.loads(metrics_path.read_text(encoding="utf-8"))
        if metrics_path.exists()
        else {"systems": {}, "reliability": {}, "news": {}, "abstentions": {}}
    )
    _figures(run_dir / FIGURES_DIR, manifest, metrics)
    cfg = manifest.config
    ev = cfg["evaluation"]
    horizons = [str(h) for h in sorted(ev["horizons_days"], reverse=True)]
    requested = manifest.systems_requested
    shown = [s for s in SYSTEMS if s in requested or s in manifest.systems]
    ran = [s for s in shown if _ran(_status_of(manifest, s))]
    not_ran = [s for s in SYSTEMS if s not in ran]
    snap = manifest.snapshot_manifest
    corpus = manifest.corpus_manifest
    out: list[str] = []
    w = out.append

    w(f"# Backtest report: {manifest.name}\n")
    w(
        f"Run status: **{manifest.status}**. Created {manifest.created_at.isoformat()}"
        + (f", finished {manifest.finished_at.isoformat()}" if manifest.finished_at else "")
        + ". Every number below comes from this run's `manifest.json` and `metrics.json`; "
        "the per-forecast table is `forecasts.csv`.\n"
    )
    if manifest.git_dirty:
        w(f"**{dirty_notice(manifest)}**\n")
    if manifest.status == "partial":
        w(
            f"This run is **partial**. It stopped because {manifest.progress.stopped_reason}. "
            f"It scored {len(manifest.progress.points_done)} point-and-system pairs and left "
            f"{len(manifest.progress.points_not_run)} unscored; it ran "
            f"{len(manifest.progress.triggers_done)} trading triggers and left "
            f"{len(manifest.progress.triggers_not_run)} unrun. The manifest lists each one.\n"
        )

    # -- what was run
    w("## What was run and what was not\n")
    w(
        _table(
            ["System", "Status"],
            [[SYSTEM_LABELS[s], _status_of(manifest, s)] for s in SYSTEMS],
        )
    )
    w("")
    if any(_status_of(manifest, s).startswith("not run: no API key") for s in SYSTEMS):
        w(
            'The systems marked "not run: no API key" need a model, and no Anthropic API key '
            "was available when this run was made. They produced no forecasts, and this report "
            "contains no number from them. Only the market's own price was scored.\n"
        )
    if manifest.models_source == "supplied":
        w(
            "The models in this run were supplied by the caller, not the live gateway (this is "
            "how the offline tests run). Treat its numbers as a test of the harness, not as "
            "model results.\n"
        )

    # -- configuration
    w("## Configuration\n")
    w(
        f"The full configuration, model assignments, snapshot and corpus manifests, plan and "
        f"progress are in `{MANIFEST_FILE}` in this directory.\n"
    )
    w(
        f"- Window: forecasts from {manifest.window_start.isoformat()} to "
        f"{manifest.window_end.isoformat()}. The latest listed knowledge cutoff of any "
        f"assigned model is {_latest_cutoff(manifest)}.\n"
        f"- Horizons: {', '.join(horizons)} day(s) before each event's close.\n"
        f"- Bootstrap: {ev['bootstrap_samples']} resamples of whole events, seed {manifest.seed}, "
        "95% percentile intervals.\n"
        f"- Commit: `{manifest.git_commit or 'not available'}`"
        + (
            f" (working tree had uncommitted changes in {len(manifest.git_changes)} path(s), "
            "listed in the manifest)"
            if manifest.git_dirty and manifest.git_changes
            else " (working tree had uncommitted changes)"
            if manifest.git_dirty
            else ""
        )
        + ".\n"
        f"- Data: snapshot content SHA-256 "
        f"`{manifest.snapshot_content_sha256 or 'not recorded'}` (the same for any rebuild "
        "that holds the same rows), snapshot file SHA-256 "
        f"`{manifest.snapshot_sha256 or 'not recorded'}` (this exact file; differs on every "
        f"rebuild), corpus texts digest `{manifest.corpus_hashes_digest or 'not recorded'}`.\n"
        f"- Snapshot: {snap.get('counts', {}).get('events', '?')} events, "
        f"{snap.get('counts', {}).get('markets', '?')} markets, built "
        f"{snap.get('built_at', '?')} from {snap.get('source_host', '?')}.\n"
        f"- Corpus: {', '.join(f'{v} from {k}' for k, v in corpus.get('counts', {}).items())}.\n"
        f"- Scoring points: {manifest.plan.scoring_points}; trading triggers: "
        f"{manifest.plan.triggers}"
        + (f"; subsample limit {manifest.plan.limit}" if manifest.plan.limit else "")
        + ".\n"
        f"- Dollar cap for the run: ${cfg['budget']['max_usd_per_run']}. Spent on new model "
        f"calls: ${float(manifest.usage.cost_usd):.4f} ({manifest.usage.live_answers} new "
        f"answers, {manifest.usage.cache_answers} from the cache).\n"
    )
    w("")
    w(
        _table(
            ["Slot", "Model", "Effort", "Listed knowledge cutoff"],
            [
                [slot, info.model, info.effort, str(info.knowledge_cutoff or "not listed")]
                for slot, info in manifest.models.items()
            ],
        )
    )
    w("")
    if manifest.plan.left_out_points or manifest.plan.left_out_triggers:
        w(
            f"The subsample left out {len(manifest.plan.left_out_points)} scoring points and "
            f"{len(manifest.plan.left_out_triggers)} triggers; the manifest names them.\n"
        )
    _left_out(w, manifest, metrics)

    # -- sample sizes
    w("## Sample sizes\n")
    w(
        "Markets within one event are correlated (they resolve on the same number), so the "
        "event count is the honest sample size and the market count is what a naive reading "
        "would claim. Both are shown side by side.\n"
    )
    rows = []
    for s in shown:
        entry = metrics["systems"].get(s)
        if not entry or not entry["by_horizon"]:
            rows.append([SYSTEM_LABELS[s], "all", "not run", "not run", "not run", "not run"])
            continue
        for h in ["all", *horizons]:
            block = entry["by_horizon"].get(h)
            if not block:
                continue
            sample = block["answered"]["sample"]
            rows.append(
                [
                    _label(manifest, s),
                    h if h == "all" else f"{h}d",
                    str(block["n_rows"]),
                    str(sample["n_markets"]),
                    str(sample["n_events"]),
                    str(sample["n_abstained"]),
                ]
            )
    w(
        _table(
            ["System", "Horizon", "Forecasts", "Markets answered", "Events answered", "Abstained"],
            rows,
        )
    )
    w("")

    # -- brier and skill
    w("## Brier score and Brier skill against the market\n")
    w(
        "The Brier score is the mean of (probability - outcome)^2; lower is better. Brier skill "
        "is 1 - system Brier / market Brier on the same rows; above zero beats the market price, "
        "below zero is worse than it. Intervals are 95% bootstrap intervals that resample whole "
        'events. "Answered" scores only the forecasts a system made; "abstentions as market" '
        "scores every abstention as if the system had forecast the market price, so a system "
        "cannot flatter itself by abstaining on hard questions. The market compared with itself "
        "has skill 0 by definition.\n"
    )
    w(f"![Brier score by horizon]({FIGURES_DIR}/{FIGURES[0]})\n")
    rows = []
    for s in shown:
        entry = metrics["systems"].get(s)
        if not entry or not entry["by_horizon"]:
            rows.append(
                [SYSTEM_LABELS[s], "all", "not run", "not run", "not run", "not run", "not run"]
            )
            continue
        for h in ["all", *horizons]:
            block = entry["by_horizon"].get(h)
            if not block:
                continue
            a, m = block["answered"], block["abstain_as_market"]
            rows.append(
                [
                    _label(manifest, s),
                    h if h == "all" else f"{h}d",
                    _interval(a["brier"]),
                    _num(a["market_brier"]),
                    _interval(a["skill"]),
                    _interval(m["brier"]),
                    _interval(m["skill"]),
                ]
            )
    w(
        _table(
            [
                "System",
                "Horizon",
                "Brier (answered)",
                "Market Brier (same rows)",
                "Skill (answered)",
                "Brier (abstentions as market)",
                "Skill (abstentions as market)",
            ],
            rows,
        )
    )
    w("")
    cut_short = [s for s in shown if _short_points(manifest, s)]
    if cut_short:
        w(
            "This run is partial, so the systems were scored on different sets of points: "
            + "; ".join(f"{SYSTEM_LABELS[s]} on {_short_points(manifest, s)}" for s in cut_short)
            + ". Their rows and the figure are not comparable with the other systems' rows; each "
            "skill is computed against the market on that system's own rows.\n"
        )
    if metrics.get("abstentions"):
        abst = {s: v for s, v in metrics["abstentions"].items() if v}
        if abst:
            w("Abstentions by reason (scheduled forecasts):\n")
            w(
                _table(
                    ["System", "Reason", "Forecast rows"],
                    [[SYSTEM_LABELS[s], r, str(n)] for s, v in abst.items() for r, n in v.items()],
                )
            )
            w("")
    if metrics.get("news"):
        w("News-triggered forecasts from the trading simulation, scored the same way:\n")
        rows = []
        for s, block in metrics["news"].items():
            a, m = block["answered"], block["abstain_as_market"]
            rows.append(
                [
                    SYSTEM_LABELS[s],
                    str(block["n_rows"]),
                    str(a["sample"]["n_events"]),
                    _interval(a["brier"]),
                    _interval(a["skill"]),
                    _interval(m["skill"]),
                ]
            )
        w(
            _table(
                [
                    "System",
                    "Forecasts",
                    "Events",
                    "Brier (answered)",
                    "Skill (answered)",
                    "Skill (abstentions as market)",
                ],
                rows,
            )
        )
        w("")

    _error_concentration(w, manifest, metrics, shown)

    # -- quote age
    quotes = metrics.get("quotes")
    if quotes and "quote_age_hours" in quotes:
        qa, ta = quotes["quote_age_hours"], quotes["last_trade_age_hours"]
        w("## Where the market price came from\n")
        w(
            f"The market price at an as-of time is {MARKET_PRICE_RULE}, read from the latest "
            "candle at or before that time. One-minute candles exist only for minutes in which "
            "the quote or a trade changed, so the quote's age below is the time since it last "
            "changed, not a sign that it had lapsed: a quote that stands unchanged is still the "
            "market's quote. The last trade's age is shown apart, for the prices that used it; "
            "an old trade outside the standing quote is clamped to the quote.\n"
        )
        w(
            _table(
                ["Measure", "Value"],
                [
                    ["Market prices scored", str(quotes["n"])],
                    ["From the bid-ask mid (both sides quoted)", str(quotes["from_mid"])],
                    [
                        "From the last trade (one side of the quote empty)",
                        str(quotes["from_last_trade"]),
                    ],
                    [
                        "of which clamped into the standing quote",
                        str(quotes["last_trade_clamped"]),
                    ],
                    ["Quote age, median (hours since it last changed)", _num(qa["median"], 1)],
                    ["Quote age, largest (hours)", _num(qa["max"], 1)],
                    ["Last-trade age, median (hours, last-trade prices)", _num(ta["median"], 1)],
                    ["Last-trade age, largest (hours)", _num(ta["max"], 1)],
                    [
                        "Last trades older than 24 hours (share of last-trade prices)",
                        _num(ta["share_older_than_24h"], 3),
                    ],
                ],
            )
        )
        w("")

    # -- reliability
    w("## Reliability\n")
    w(f"![Reliability diagram]({FIGURES_DIR}/{FIGURES[1]})\n")
    for s in shown:
        table = metrics["reliability"].get(s, {}).get("answered")
        if not table:
            w(f"{SYSTEM_LABELS[s]}: not run.\n")
            continue
        w(
            f"{SYSTEM_LABELS[s]}, answered forecasts, all horizons (expected calibration error {_num(table['ece'])}):\n"
        )
        w(
            _table(
                ["Bin", "Forecasts", "Mean forecast", "Share YES"],
                [
                    [
                        f"[{b['lower']:.1f}, {b['upper']:.1f})",
                        str(b["count"]),
                        _num(b["mean_forecast"], 3) if b["count"] else "empty",
                        _num(b["observed_frequency"], 3) if b["count"] else "empty",
                    ]
                    for b in table["bins"]
                ],
            )
        )
        w("")

    # -- trading
    w("## Simulated trading after fees\n")
    pipe = _status_of(manifest, PIPELINE)
    pnl = metrics.get("pnl", {})
    summary = pnl.get("summary", {})
    w(f"![Equity curve]({FIGURES_DIR}/{FIGURES[2]})\n")
    trading_not_run = _trading_not_run(manifest)
    if trading_not_run:
        w(
            "The trading simulation runs the full pipeline on every news trigger. It did not "
            f"run in this run ({trading_not_run}), so there is no trading and no P&L to report.\n"
        )
    else:
        dd = pnl.get("max_drawdown", {})
        w(
            _table(
                ["Measure", "Value"],
                [
                    ["Bankroll (notional)", _money(pnl.get("bankroll"))],
                    ["Fills", str(summary.get("n_trades", 0))],
                    ["Settled fills", str(summary.get("n_settled", 0))],
                    ["Realized P&L after fees", _money(summary.get("total"))],
                    ["Not trading", _money(0)],
                    ["P&L 95% interval (resampling events)", _pnl_interval(pnl.get("interval"))],
                    ["Fees paid on settled fills", _money(summary.get("fees"))],
                    ["Still open at the end (cost)", _money(summary.get("open_cost"))],
                    [
                        "Maximum drawdown",
                        f"{float(dd.get('fraction', 0)) * 100:.2f}% ({_money(dd.get('dollars'))})",
                    ],
                ],
            )
        )
        w("")
        trading_halt = (metrics.get("self_halts") or {}).get("trading")
        if trading_halt:
            w(
                f"The trading pass halted itself at {trading_halt['first_started_at']}, and "
                "nobody resumes a halt in a backtest, so these figures describe the run only "
                "up to then (see Exits by reason).\n"
            )

    # -- routing
    w("## Routing tiers\n")
    w(
        "With no human present in a backtest, approve-tier proposals are filled and tagged "
        "(fill-tagged). The stage is below autonomous, so even proposals that met every auto "
        "condition (would-be-auto) went to the approve tier; they are counted apart from the rest.\n"
    )
    eps = metrics.get("episodes", {})
    if not eps or not any(t.get("n_episodes") for t in eps.values()):
        w("No pipeline episode ran, so there are no routing tiers to report.\n")
    else:
        if (eps.get("scoring") or {}).get("n_episodes"):
            w(SCORING_PASS_NOTE + "\n")
        rows = []
        for name, t in eps.items():
            rows.append(
                [
                    name,
                    str(t["n_episodes"]),
                    ", ".join(f"{k}: {v}" for k, v in t["routing_tiers"].items()) or "none",
                    str(t["executed_with_fills"]),
                    str(t["fill_tagged_executed"]),
                    str(t["fill_tagged_would_be_auto"]),
                    str(t["auto_executed"]),
                ]
            )
        w(
            _table(
                [
                    "Pass",
                    "Episodes",
                    "Routing tiers",
                    "Executed with fills",
                    "Fill-tagged approve tier",
                    "of which would-be-auto",
                    "Auto tier",
                ],
                rows,
            )
        )
        w("")
        by = pnl.get("by_routing", {})
        if by:
            w(
                _table(
                    ["Trades", "Fills", "Realized P&L after fees"],
                    [
                        [k.replace("_", " "), str(v["n_fills"]), _money(v["total"])]
                        for k, v in by.items()
                    ],
                )
            )
            w("")

        # -- exits and gate
        w("## Exits by reason\n")
        halts = metrics.get("self_halts") or {}
        for name, t in eps.items():
            w(f"{name.capitalize()} pass, {t['n_episodes']} episode(s):\n")
            w(_counts_table(t["exits"], "exit"))
            w("")
            if halts.get(name):
                w(_self_halt_sentence(name, halts[name]))
        w("## Gate rejections by reason\n")
        for name, t in eps.items():
            w(f"{name.capitalize()} pass, {t['n_gate_rejected']} rejected proposal(s):\n")
            w(_counts_table(t["gate_rejections"], "failed check"))
            w("")
        w("## Cost, calls and latency per episode\n")
        rows = []
        for name, t in eps.items():
            c, n, s = t["cost_usd"], t["calls"], t["latency_seconds"]
            rows.append(
                [
                    name,
                    str(t["n_episodes"]),
                    f"{_money(c['mean'])} / {_money(c['median'])} / {_money(c['max'])}",
                    f"{_num(n['mean'], 1)} / {_num(n['median'], 1)} / {_num(n['max'], 1)}",
                    f"{_num(s['mean'], 1)} / {_num(s['median'], 1)} / {_num(s['max'], 1)}",
                ]
            )
        w(
            _table(
                [
                    "Pass",
                    "Episodes",
                    "Cost mean / median / max",
                    "Calls mean / median / max",
                    "Model seconds mean / median / max",
                ],
                rows,
            )
        )
        w("")
        w(
            "Cost per episode is what its answers cost when they were first made; an answer "
            "from the cache costs the run nothing new.\n"
        )

    # -- pruning
    w("## Pruning audit\n")
    pruning = metrics.get("pruning", {}).get("summary", {})
    if not pruning or not pruning.get("n_episodes"):
        w(
            "No resolved episode had a scenario tree to audit"
            + (" (the pipeline did not run)." if not _ran(pipe) else ".")
            + "\n"
        )
    else:
        w(
            f"Over {pruning['n_episodes']} resolved episode(s) with a scenario tree, the mean "
            f"share of the posterior (given the outcome that happened) on frozen scenarios was "
            f"{_num(pruning['mean_frozen_posterior_mass'], 3)}, and the most likely scenario "
            f"given the outcome was a frozen one in {_num(pruning['frozen_most_likely_rate'], 3)} "
            "of them.\n"
        )

    # -- fills
    w("## How each fill was priced\n")
    fp = metrics.get("fill_pricing", {})
    if not fp or not fp.get("orders"):
        w(
            "No order was placed"
            + (" (the pipeline did not run)" if not _ran(pipe) else "")
            + ". Episodes that ended stale_price because the as-of book offered nothing at or "
            f"below the limit: {fp.get('stale_price_exits', 0)}.\n"
        )
    else:
        labels = {
            "minute": "One-minute candle",
            "hour": "Hourly fallback (coarse)",
            "none": "Empty book",
        }
        w(
            _table(
                ["Book priced from", "Orders", "Orders with a fill"],
                [
                    [
                        labels[g],
                        str(fp["by_granularity"].get(g, 0)),
                        str(fp["filled_by_granularity"].get(g, 0)),
                    ]
                    for g in ("minute", "hour", "none")
                ],
            )
        )
        w("")
        w(
            f"A further {fp.get('stale_price_exits', 0)} episode(s) sent no order because the "
            "as-of book offered nothing at or below the limit (exit stale_price); that check "
            "never reads the book an order fills against.\n"
        )
    late = metrics.get("late_starts")
    if late:
        w(_late_start_sentence(late))

    # -- leakage
    w("## How look-ahead was prevented\n")
    w("The controls the harness puts in place whenever the systems that read evidence run:\n")
    for line in manifest.leakage_controls:
        w(f"- {line}")
    w("")
    w(
        f"In this run the window starts {manifest.window_start.isoformat()} and every assigned "
        "model lists a knowledge cutoff before it (table above). " + _canary_sentence(manifest)
    )

    # -- fills assumptions
    w("## Fill assumptions\n")
    for line in manifest.fill_assumptions:
        w(f"- {line}")
    w("")

    # -- limitations
    w("## Limitations\n")
    scored = metrics.get("scored_events")
    if scored is None:  # a run made before the scored events were counted
        n_events = f"{snap.get('counts', {}).get('events', 'a handful of')} settled events"
        held = f"The snapshot holds only {n_events}"
    else:
        per_series: dict[str, int] = {}
        for ticker in scored:
            series = ticker.split("-", 1)[0]
            per_series[series] = per_series.get(series, 0) + 1
        held = f"Scoring covered only {len(scored)} settled event(s)" + (
            " (" + ", ".join(f"{n} {k}" for k, n in sorted(per_series.items())) + ")"
            if per_series
            else ""
        )
    if manifest.plan.releases_without_event:
        held += (
            f"; {len(manifest.plan.releases_without_event)} release(s) in the window had no "
            "event in the snapshot (see Left out of scoring)"
        )
    w(
        f"- **This is a pilot.** {held}. That is a handful "
        "per series, and each carries several correlated markets (five per Fed meeting, "
        "twenty or more per CPI release). The events, not the markets, are the independent "
        "observations, so the intervals are wide and no difference here separates skill from "
        "luck.\n"
        f"- The market price is {MARKET_PRICE_RULE}, read from candles. Far strikes "
        "often have one side of the quote empty, so their price is a last trade (kept inside "
        "the standing quote); markets priced at 0.01 or 0.99 dominate the row count and pull "
        "every Brier score toward zero.\n"
        "- Fills are simulated from candles without depth data, some from the coarse hourly "
        "fallback or against an empty book (counted under How each fill was priced). "
        "Simulated P&L is not evidence of tradable edge.\n"
        "- Forecasts at 7, 3 and 1 days before close of the same event are not independent of "
        "each other either; the bootstrap keeps an event's rows together across horizons only "
        "in the pooled row.\n"
    )
    if not_ran:
        w(
            "- Not run in this run: "
            + "; ".join(f"{SYSTEM_LABELS[s]} ({_status_of(manifest, s)})" for s in not_ran)
            + ". Nothing in this report describes how those systems would do.\n"
        )

    # -- files
    w("## Files\n")
    w(
        "- `manifest.json`: configuration, models, window, data manifests, plan, progress.\n"
        "- `forecasts.csv`: one row per system, market and as-of time, with the abstention note.\n"
        "- `metrics.json`: every number above.\n"
        f"- `{FIGURES_DIR}/`: the three figures.\n"
        + (
            "- `traces/`: saved episodes (recorded world, cassette, and the episode with its "
            "full trace). Each was replayed offline right after it was saved; the result:\n"
            + "".join(f"  - `{name}`: {result}\n" for name, result in manifest.traces.items())
            if manifest.traces
            else "- No episode traces were saved (no pipeline episode ran).\n"
        )
    )
    path = run_dir / REPORT_FILE
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return path


RELEASE_LINE = re.compile(r", (\S+)\): no (\S+) event in the snapshot$")
"""The release time and series at the end of a ``releases_without_event`` line."""

DROPPED_CLOSE = re.compile(r"^closes (\S+),")
"""The close time at the start of a dropped event's reason, when the builder knew it."""

NUMBER_WORDS = {1: "a", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}


def _span(delta: timedelta) -> str:
    """A time span in words, such as ``two days`` or ``a day``, for the report's sentences."""
    seconds = delta.total_seconds()
    for unit, size in (("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = int(seconds // size)
            return f"{NUMBER_WORDS.get(n, str(n))} {unit}" + ("" if n == 1 else "s")
    return f"{seconds:g} seconds"


def _release_verdict(line: str, dropped: dict[str, str]) -> str:
    """What the dropped events say about one release without an event, in one sentence.

    Only a dropped event of the release's series that closes within
    ``RELEASE_MATCH`` of the release is named as possibly its event: the same
    rule the schedule uses to match an event to a release, so the two cannot
    disagree. One without a close time on record can be neither matched nor
    ruled out. Otherwise the exchange listed no event for it.
    """
    found = RELEASE_LINE.search(line)
    if found is None:
        return ""
    released, series = datetime.fromisoformat(found.group(1)), found.group(2)
    within = f"within {_span(RELEASE_MATCH)}"
    of_series = {t: r for t, r in dropped.items() if t.split("-", 1)[0] == series}
    near, unknown = [], []
    for ticker, reason in of_series.items():
        close = DROPPED_CLOSE.match(reason)
        if close is None:
            unknown.append(ticker)
        elif abs(datetime.fromisoformat(close.group(1)) - released) <= RELEASE_MATCH:
            near.append(ticker)
    if near:
        return (
            f"The snapshot builder dropped {', '.join(near)}, which closes {within} of the "
            f"release (listed below): it may be this release's {series} event."
        )
    if unknown:
        return (
            f"No {series} event the builder dropped closes {within} of it, but "
            f"{', '.join(unknown)} has no close time on record (listed below), so whether it "
            "belongs to this release is not known."
        )
    if of_series:
        return (
            f"So the exchange listed no {series} event for this release: none of the "
            f"{len(of_series)} {series} event(s) the builder dropped closes {within} of it."
        )
    return (
        f"So the exchange listed no {series} event for this release: the builder dropped no "
        f"{series} event."
    )


def _left_out(w: Any, manifest: Any, metrics: dict[str, Any]) -> None:
    """The "Left out of scoring" section: every release, event, point, market and row not scored."""
    plan = manifest.plan
    snap = manifest.snapshot_manifest
    dropped = snap.get("dropped_events")
    skipped_markets = metrics.get("skipped_markets") or []
    unresolved = metrics.get("unresolved_rows") or []
    w("## Left out of scoring\n")
    if not (
        plan.releases_without_event
        or dropped
        or plan.skipped_events
        or plan.skipped_points
        or skipped_markets
        or unresolved
    ):
        w(
            "Nothing in the window was left out: every release in the calendar has an event in "
            "the snapshot, and every scheduled point and every priced market with a result was "
            "scored"
            + (
                ""
                if dropped is not None
                else " (this snapshot predates the record of events its builder dropped)"
            )
            + ".\n"
        )
        return
    if plan.releases_without_event:
        w(
            f"The release calendar has {len(plan.releases_without_event)} release(s) in the "
            "window with no event of the followed series in the snapshot, so nothing was "
            "scored for them. The exchange does not list an event for every release.\n"
        )
        for line in plan.releases_without_event:
            verdict = "" if dropped is None else " " + _release_verdict(line, dropped)
            w(f"- {line}.{verdict}")
        w("")
        if dropped is None:
            w(
                "This snapshot was built before its builder recorded the events it dropped, "
                "so the snapshot alone does not show whether the exchange listed an event for "
                "these releases.\n"
            )
    if dropped:
        w(f"Events the exchange listed that the snapshot builder did not store ({len(dropped)}):\n")
        for ticker, reason in dropped.items():
            w(f"- {ticker}: {reason}")
        w("")
    if plan.skipped_events:
        w(f"Events in the snapshot that were not scored ({len(plan.skipped_events)}):\n")
        for line in plan.skipped_events:
            w(f"- {line}")
        w("")
    if plan.skipped_points:
        w(
            "Scoring points that do not exist (before the window start or before the event "
            "opened): " + "; ".join(plan.skipped_points) + ".\n"
        )
    if skipped_markets:
        shown = skipped_markets[:20]
        w(
            f"Markets with no price at a scoring point, so no system has a row for them there "
            f"({len(skipped_markets)}"
            + (
                f"; the first {len(shown)} below, all in `metrics.json`"
                if len(shown) < len(skipped_markets)
                else ""
            )
            + "):\n"
        )
        for line in shown:
            w(f"- {line}")
        w("")
    if unresolved:
        w(
            f"Priced markets left out because the snapshot holds no final result for them: "
            f"{len(unresolved)} market forecast(s), listed in `metrics.json` under "
            "`unresolved_rows`.\n"
        )


def _percent(share: Any) -> str:
    return "not defined" if share is None else f"{float(share) * 100:.1f}%"


def _error_concentration(w: Any, manifest: Any, metrics: dict[str, Any], shown: list[str]) -> None:
    """The "Where the error comes from" section, from each system's ``error_concentration`` block.

    A Brier score is a mean, so a handful of confident misses can decide it.
    This says how much of each system's summed squared error comes from rows
    priced near 0 or 1 and from its largest event and markets, so no reading
    of the scores has to guess. Left out for a run whose metrics predate it.
    """
    blocks = {
        s: metrics["systems"][s]["error_concentration"]
        for s in shown
        if (metrics["systems"].get(s) or {}).get("error_concentration")
    }
    if not blocks:
        return
    w("## Where the error comes from\n")
    w(
        "Each system's summed squared error, (probability - outcome)^2 added over the rows of "
        "its pooled Brier score (answered forecasts, all horizons), split by the forecast "
        "probability and by event and market. When one event carries a large share, a "
        "comparison on this sample rests heavily on that event; the event-clustered interval "
        "above already reflects it.\n"
    )
    for s, block in blocks.items():
        total = float(block["squared_error"])
        w(
            f"{_label(manifest, s)}: summed squared error {_num(total)} over "
            f"{block['n_rows']} row(s).\n"
        )
        if total <= 0:
            w("Every forecast on these rows matched its outcome, so there is no error to split.\n")
            continue
        w(
            _table(
                ["Forecast probability", "Rows", "Squared error", "Share of the error"],
                [
                    [b["name"], str(b["n_rows"]), _num(b["squared_error"]), _percent(b["share"])]
                    for b in block["bands"]
                ],
            )
        )
        w("")
        top = block["top_event"]
        w(
            f"The event with the largest share is {top['name']}: {_percent(top['share'])} of "
            f"the error, from {top['n_rows']} row(s). The markets with the largest shares: "
            + "; ".join(
                f"{m['name']} {_percent(m['share'])} ({m['n_rows']} row(s))"
                for m in block["top_markets"]
            )
            + ".\n"
        )


SCORING_PASS_NOTE = (
    "The scoring pass exists to score forecasts. Before each scoring point the outcome resolver "
    "writes back every result the snapshot exchange shows as settled at the point's as-of "
    "time. The pipeline's episodes size, gate and fill on a paper account that settles those "
    "markets, its stops read them, and its critic is given earlier forecasts that resolved "
    "before then (counted in whole seconds) as calibration examples; the market and the "
    "one-call baselines read no "
    "outcome. The points follow the calendar, not the news, and never wait for an earlier "
    "order's outcome, so the pass's tiers and its exits after the forecast step say little "
    "about trading, and its orders are not in the trading results. The trading pass is the "
    "trading simulation."
)
"""Printed above the first per-pass table, so the scoring pass's rows are not read as trading."""


def _self_halt_sentence(name: str, halt: dict[str, Any]) -> str:
    """What a self-halt in one pass means for that pass's results, in plain sentences."""
    head = (
        f"The first {name} episode to end halted started at {halt['first_started_at']} "
        f"(trigger {halt['first_trigger_id']}). Its reason: {halt['reason']}. From then on, "
        f"that episode included, {halt['n_halted_from_then']} of the {halt['n_from_then']} "
        f"{name} episode(s) ended halted. Nobody resumes a halt in a backtest."
    )
    if name == "trading":
        return (
            head + " From that time the simulation still forecasts but places no further "
            "orders, so the trading results describe the run only up to the halt.\n"
        )
    return (
        head + " The halt stops only orders: each forecast is made before the gate, so "
        "every scoring point is still forecast and scored.\n"
    )


def _duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} seconds"
    if seconds < 7_200:
        return f"{seconds / 60:.1f} minutes"
    return f"{seconds / 3_600:.1f} hours"


def _late_start_sentence(late: dict[str, Any]) -> str:
    """How many trading episodes started after their trigger's time, and the longest wait."""
    n, of = int(late.get("episodes", 0)), int(late.get("of", 0))
    if not n:
        return f"None of the {of} trading episode(s) started later than its trigger's time.\n"
    return (
        f"{n} of the {of} trading episode(s) started later than their trigger's time, the "
        f"longest by {_duration(float(late.get('longest_seconds', 0)))}: the simulation waits "
        "for the candle that priced the previous order to end before the next episode starts.\n"
    )


def _canary_sentence(manifest: Any) -> str:
    """What the canary actually guarded in this run, from the manifest's counts."""
    if manifest.status == "aborted":
        return "The run was aborted: " + manifest.progress.stopped_reason + "\n"
    checks = manifest.leakage_checks
    if not checks:
        return (
            "This run's manifest does not record how many retrievals and episodes were "
            "checked for the canary.\n"
        )
    retrievals, episodes = checks.get("retrievals_checked", 0), checks.get("episodes_checked", 0)
    if not checks.get("index_built"):
        return (
            "No system in this run reads evidence, so no evidence index was built and no "
            "retrieval was made: the canary was not exercised, and the retrieval controls "
            "above did not come into play. The market's price is read from the snapshot as "
            "of each as-of time.\n"
        )
    published = str(checks.get("canary_published_at") or "")[:10]
    return (
        f"The canary guarded {retrievals} retrieval call(s) and {episodes} finished "
        "episode(s); none returned it, and a single hit would have aborted the run. The "
        f"canary is dated {published or 'after the window'}, after every as-of time in the "
        "run, so it catches an as-of filter that is missing altogether; a filter that lets "
        "through documents only hours or days late would pass it unnoticed.\n"
    )


def _latest_cutoff(manifest: Any) -> str:
    cutoffs = [i.knowledge_cutoff for i in manifest.models.values() if i.knowledge_cutoff]
    return max(cutoffs).isoformat() if cutoffs else "not listed"


def _pnl_interval(block: dict[str, Any] | None) -> str:
    if not block:
        return "not defined"
    lo, hi = block.get("lower"), block.get("upper")
    if lo is None or hi is None:
        return "not defined"
    return f"[{_money(lo)}, {_money(hi)}]"


def dirty_notice(manifest: Any) -> str:
    """The plain statement a run made from a tree with uncommitted changes carries."""
    changed = manifest.git_changes
    listed = (
        f" ({len(changed)} path(s) changed or untracked: "
        + ", ".join(changed[:10])
        + (", ..." if len(changed) > 10 else "")
        + ")"
        if changed
        else ""
    )
    return (
        "This run was made from a working tree with uncommitted changes"
        + listed
        + f", so commit {manifest.git_commit or '(not available)'} does not hold exactly the "
        "code that produced it."
    )


def publish_run(
    run_dir: str | Path,
    name: str,
    *,
    root: str | Path = PUBLISH_ROOT,
    allow_supplied: bool = False,
) -> Path:
    """Copy the committed artifact set of ``run_dir`` to ``root/name`` and return it.

    The set is the manifest, the per-forecast table, the metrics, the report with
    its figures, and the saved traces. The report is built first if missing.
    The target directory is replaced, so it holds exactly this set, and the
    published manifest's ``outputs`` lists exactly what was copied. A run made
    from a tree with uncommitted changes is published with that said in its
    manifest notes and in a logged warning (and in the report).

    A run whose models were supplied by the caller (scripted answers in a test,
    or a rehearsal) is not a result, so publishing it is refused with a
    ``ValueError`` before anything is written, unless ``allow_supplied`` is set.
    The command line never sets it.
    """
    import logging

    run_dir = Path(run_dir)
    if read_manifest(run_dir).models_source == "supplied" and not allow_supplied:
        raise ValueError(
            "this run's answers came from models supplied by the caller (scripted answers "
            "or a rehearsal), not from the live gateway, so it is not a result and is not "
            "published"
        )
    if not (run_dir / REPORT_FILE).exists():
        build_report(run_dir)
    target = Path(root) / name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    for file in COMMITTED:
        shutil.copy2(run_dir / file, target / file)
    shutil.copytree(run_dir / FIGURES_DIR, target / FIGURES_DIR)
    traces = run_dir / "traces"
    if traces.is_dir() and any(traces.iterdir()):
        shutil.copytree(traces, target / "traces")
    manifest = read_manifest(target)
    outputs = [
        *COMMITTED,
        *(f"{FIGURES_DIR}/{p.name}" for p in sorted((target / FIGURES_DIR).iterdir())),
        *(
            f"traces/{p.name}"
            for p in (sorted((target / "traces").iterdir()) if (target / "traces").is_dir() else [])
        ),
    ]
    notes = list(manifest.notes)
    if manifest.git_dirty:
        notice = dirty_notice(manifest)
        logging.getLogger(__name__).warning("publishing %s: %s", name, notice)
        if notice not in notes:
            notes.append(notice)
    write_manifest(manifest.model_copy(update={"outputs": outputs, "notes": notes}), target)
    return target


__all__ = [
    "COMMITTED",
    "FIGURES",
    "PUBLISH_ROOT",
    "REPORT_FILE",
    "build_report",
    "dirty_notice",
    "publish_run",
]
