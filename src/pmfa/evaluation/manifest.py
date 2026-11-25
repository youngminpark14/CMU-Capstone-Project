"""The run manifest: everything needed to trace a reported number back to what produced it.

Written to ``manifest.json`` before a run does anything and rewritten when it
ends: the full configuration, the model and effort of every slot with each
model's listed knowledge cutoff, the date window, the snapshot and corpus
manifests, the git commit, the seed, the systems with what happened to each,
the plan with its counts, the leakage and cost controls in force, and, for a
run that stopped early, what was and was not run.
"""

from __future__ import annotations

import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pmfa.config import Config
from pmfa.records.base import Record, UtcTime

MANIFEST_FILE = "manifest.json"

RunStatus = Literal["running", "complete", "partial", "dry_run", "aborted"]


class SlotInfo(Record):
    model: str
    effort: str
    max_tokens: int
    knowledge_cutoff: date | None


class SystemPlan(Record):
    """The model calls one system would make, and what they would cost."""

    system: str
    status: str
    """``run``, ``not requested``, ``not run: no API key``, ``not run: <reason>``."""
    forecasts: int = 0
    """Scoring points (plus triggers, for the pipeline) the system forecasts."""
    model_calls_max: int = 0
    """An upper bound on model calls."""
    model_calls_min: int = 0
    """A floor: what the run makes even if every episode stops early."""
    usd_max: Decimal = Decimal("0")
    usd_min: Decimal = Decimal("0")
    notes: list[str] = []


class Plan(Record):
    scoring_points: int
    triggers: int
    left_out_points: list[str] = []
    left_out_triggers: list[str] = []
    skipped_points: list[str] = []
    skipped_events: list[str] = []
    limit: int | None = None
    score: bool = True
    trade: bool = True
    systems: list[SystemPlan] = []
    usd_max: Decimal = Decimal("0")
    usd_min: Decimal = Decimal("0")
    assumptions: list[str] = []


class Progress(Record):
    """What a run got through. A partial run lists what it did not run."""

    points_done: list[str] = []
    """``point_id/system`` pairs scored."""
    points_not_run: list[str] = []
    triggers_done: list[str] = []
    triggers_not_run: list[str] = []
    stopped_reason: str = ""


class RunUsage(Record):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    """New spending in this run; answers from the response cache cost nothing."""
    cache_answers: int = 0
    live_answers: int = 0


class RunManifest(Record):
    run_id: str
    name: str
    status: RunStatus
    created_at: UtcTime
    finished_at: UtcTime | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    seed: int
    window_start: date
    window_end: UtcTime
    systems_requested: list[str]
    systems: dict[str, str]
    """System -> what happened: ``run``, ``partial: ...``, ``not run: no API key``, ..."""
    models: dict[str, SlotInfo]
    """Slot -> model, effort, max_tokens, and the model's listed knowledge cutoff."""
    models_source: str
    """``gateway`` (live, cached), ``supplied`` (a ``Models`` passed in, e.g. scripted in tests), or ``none``."""
    snapshot_manifest: dict[str, Any]
    corpus_manifest: dict[str, Any]
    index: dict[str, Any] = {}
    plan: Plan
    progress: Progress = Progress()
    usage: RunUsage = RunUsage()
    leakage_controls: list[str] = []
    cost_controls: list[str] = []
    fill_assumptions: list[str] = []
    outputs: list[str] = []
    traces: dict[str, str] = {}
    """Saved trace directory -> the result of replaying it offline right after it was saved."""
    notes: list[str] = []
    config: dict[str, Any]


def slot_info(config: Config) -> dict[str, SlotInfo]:
    out = {}
    for slot, assignment in sorted(config.models.slots.items()):
        info = config.models.catalog.get(assignment.model)
        out[slot.value] = SlotInfo(
            model=assignment.model,
            effort=assignment.effort,
            max_tokens=assignment.max_tokens,
            knowledge_cutoff=info.knowledge_cutoff if info else None,
        )
    return out


def git_state(cwd: str | Path | None = None) -> tuple[str | None, bool | None]:
    """The commit checked out and whether the working tree has changes, when git is available.

    Read-only: ``git rev-parse HEAD`` and ``git status --porcelain``.
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        if commit.returncode != 0:
            return None, None
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return commit.stdout.strip() or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def write_manifest(manifest: RunManifest, run_dir: str | Path) -> Path:
    path = Path(run_dir) / MANIFEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_manifest(run_dir: str | Path) -> RunManifest:
    return RunManifest.model_validate_json(
        (Path(run_dir) / MANIFEST_FILE).read_text(encoding="utf-8")
    )


LEAKAGE_CONTROLS = [
    "The as-of rule is inside the ports: the snapshot exchange shows quotes, listings and "
    "results only up to the simulated clock, the frozen evidence port returns only documents "
    "published at or before the as-of time, and every retrieval index filters on publish or "
    "resolution time inside the Chroma query, before the nearest-neighbour search.",
    "A canary document dated one day after the end of the window, containing the configured "
    "canary text and the words every query uses, sits in the evidence index (not in the "
    "corpus). Every retrieval result and every finished episode is checked for it; one hit "
    "aborts the run.",
    "No web access: the exchange is the frozen snapshot, the evidence is the stored corpus, "
    "and no port in the backtest world holds an HTTP client.",
    "The response cache key includes the as-of time, which is part of every role input.",
    "The run refuses to start if any assigned model's listed knowledge cutoff falls on or "
    "after the window start.",
]

COST_CONTROLS = [
    "A dry run (--dry-run) prints the plan and a dollar estimate without any model call.",
    "The gateway's run meter enforces budget.max_usd_per_run; on reaching it the run stops "
    "cleanly, is marked partial, and writes outputs for what it ran.",
    "Answers are cached on disk by content (data/cache/models): a rerun of the same "
    "questions costs nothing.",
    "--limit scores an evenly spaced subsample first; the manifest lists what was left out.",
]

FILL_ASSUMPTIONS = [
    "Orders are immediate-or-cancel limit orders filled against a book built from one candle: "
    "the first one-minute candle after the as-of time plus execution.backtest_latency_seconds, "
    "at the period's worst touch (the highest YES ask, or 1 minus the lowest YES bid), with "
    "each level holding execution.backtest_volume_share of that candle's volume.",
    "One-minute candles exist only for minutes in which a quote or trade changed, so when no "
    "one-minute candle contains the arrival time the hourly candle is used (coarse), and when "
    "none exists, or the candle had no volume, the book is empty and nothing fills.",
    "The exchange's history has no order-book depth, so liquidity is a candle-volume proxy.",
    "Taker fees are paid on every fill; the unfilled remainder of an order is dropped.",
]


__all__ = [
    "COST_CONTROLS",
    "FILL_ASSUMPTIONS",
    "LEAKAGE_CONTROLS",
    "MANIFEST_FILE",
    "Plan",
    "Progress",
    "RunManifest",
    "RunStatus",
    "RunUsage",
    "SlotInfo",
    "SystemPlan",
    "git_state",
    "read_manifest",
    "slot_info",
    "write_manifest",
]
