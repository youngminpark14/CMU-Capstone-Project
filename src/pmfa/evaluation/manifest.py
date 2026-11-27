"""The run manifest: everything needed to trace a reported number back to what produced it.

Written to ``manifest.json`` before a run does anything and rewritten when it
ends: the full configuration, the model and effort of every slot with each
model's listed knowledge cutoff, the date window, the snapshot and corpus
manifests with digests of the snapshot file and the corpus texts, the git
commit with the paths that differed from it, the seed, the systems with what happened to each,
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
    releases_without_event: list[str] = []
    """Calendar releases in the window with no event of the followed series in the snapshot."""
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
    git_changes: list[str] = []
    """With ``git_dirty``: the paths changed against ``git_commit`` or untracked. The commit
    alone does not hold the code that produced a run made from a dirty tree."""
    snapshot_sha256: str | None = None
    """The SHA-256 of the snapshot file the run read."""
    snapshot_content_sha256: str | None = None
    """``SnapshotStore.content_sha256`` of the snapshot the run read: a hash of its data rows
    alone, so a snapshot rebuilt later can be shown to hold the same data. ``snapshot_sha256``
    names this exact file, but differs on every rebuild because the file also holds the build
    manifest and a layout that depends on the build. ``None``: a run recorded before it."""
    corpus_hashes_digest: str | None = None
    """``hashes_digest`` of the corpus manifest's per-document content hashes."""
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
    leakage_checks: dict[str, Any] = {}
    """What the canary guarded in this run: ``index_built``, ``canary_doc_id``,
    ``canary_published_at``, ``retrievals_checked``, ``episodes_checked``. Empty
    for a dry run, and in a manifest written before these were counted."""
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


class GitState(Record):
    """The code a run was made from: the commit, and what differed from it in the working tree."""

    commit: str | None = None
    dirty: bool | None = None
    changes: list[str] = []
    """Paths changed against the commit or untracked, when the tree is dirty (paths only)."""
    root: str | None = None


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str] | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done if done.returncode == 0 else None


def git_details(cwd: str | Path | None = None) -> GitState:
    """The commit, whether the tree is dirty, and the changed and untracked paths.

    ``cwd`` defaults to this package's own directory, so the repository asked
    is the one holding the code that runs, not whatever directory the process
    started in. Every command runs from that repository's root. Read-only:
    ``git rev-parse`` and ``git status``.
    """
    start = Path(cwd) if cwd is not None else Path(__file__).resolve().parent
    top = _git(["rev-parse", "--show-toplevel"], start)
    if top is None or not top.stdout.strip():
        return GitState()
    root = top.stdout.strip()
    commit = _git(["rev-parse", "HEAD"], root)
    if commit is None:
        return GitState(root=root)
    status = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    if status is None:
        return GitState(commit=commit.stdout.strip() or None, root=root)
    changes: list[str] = []
    fields = status.stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        changes.append(entry[3:])
        if entry[0] in "RC":  # a rename or copy is followed by its original path
            i += 1
    return GitState(
        commit=commit.stdout.strip() or None,
        dirty=bool(changes),
        changes=sorted(set(changes)),
        root=root,
    )


def git_state(cwd: str | Path | None = None) -> tuple[str | None, bool | None]:
    """The commit checked out and whether the working tree has changes (see ``git_details``)."""
    state = git_details(cwd)
    return state.commit, state.dirty


def file_sha256(path: str | Path) -> str | None:
    """The SHA-256 of a file's bytes, or ``None`` when it does not exist."""
    import hashlib

    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def hashes_digest(hashes: dict[str, str]) -> str:
    """One SHA-256 over ``id hash`` lines in id order: names a set of content hashes in one value."""
    import hashlib

    text = "".join(f"{key} {value}\n" for key, value in sorted(hashes.items()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    "Every decision in a backtest episode (the price re-read before sending, the one "
    "recompute, the second gate, routing's price move) reads the order book as of the as-of "
    "time; only the paper fill is priced from a later candle (see Fill assumptions), and no "
    "role or decision sees that book.",
    "A paper fill is timed at the end of the candle that priced it, not at the as-of time. "
    "The paper account's balance and positions, the equity points and the stops that read "
    "orders (fill failures, a changed source) count an order only from that time, so no "
    "decision made before it sees the order's price or outcome.",
    "In the trading pass the next episode starts no earlier than the latest order's outcome "
    "time, so an episode can start later than its trigger, with that later time as its as-of "
    "time; the report counts these late starts. The scoring pass keeps its fixed as-of times "
    "and relies on that time filter.",
    "When the pipeline runs, the outcome resolver runs before each scoring point and each "
    "trading episode and writes back every result the snapshot exchange shows as settled at "
    "that as-of time; nothing settled after it is read. The paper account settles those "
    "markets and the stops read them. The critic is given up to retrieval.ledger_k of the "
    "pipeline's earlier forecasts that resolved before the as-of time, same category first, "
    "and the calibration stop counts the entries resolved before it. The ledger index "
    "compares whole seconds, so a result settled in the last second before an as-of time, or "
    "at it, reaches the paper account then and the critic only at a later as-of time. The "
    "market and the one-call baselines read no outcome.",
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
    "In a backtest every decision (the price re-read before sending, the one recompute, the "
    "second gate, routing's price move) reads the order book as of the as-of time. Only the "
    "fill is priced from the first one-minute candle after the as-of time plus "
    "execution.backtest_latency_seconds (the candle whose period holds that arrival time).",
    "A fill is timed at the end of the candle that priced it: up to a minute after the "
    "arrival time on one-minute candles, up to an hour on the hourly fallback. An order that "
    "meets no candle has its outcome timed at the end of the hour searched, or at the "
    "arrival time when the market had closed by then. The paper account, the equity points "
    "and the stops that read orders count an order only from that time.",
    "In the trading pass the next episode starts no earlier than that time, so an episode can "
    "start later than its trigger (the report counts these). The scoring pass keeps its fixed "
    "as-of times and relies on that time filter.",
    "The immediate-or-cancel limit order fills against a book built from that candle on the "
    "conservative side of its period: buying YES meets its highest YES ask, buying NO meets 1 "
    "minus its lowest YES bid. Each side holds execution.backtest_volume_share of the candle's "
    "volume, floored to whole contracts; a limit that book does not meet fills nothing.",
    "One-minute candles exist only for minutes in which a quote or trade changed, so when no "
    "one-minute candle holds the arrival time the hourly candle holding it is used, and that "
    "order is flagged coarse (see How each fill was priced). When neither exists, the market "
    "has closed by the arrival time, or the candle's volume times "
    "execution.backtest_volume_share is under one contract, the book is empty and nothing "
    "fills. A side is empty when the candle has no quote to price it from: buying YES needs "
    "a YES ask below 1, buying NO a YES bid above 0.",
    "The exchange's history has no order-book depth, so liquidity is a candle-volume proxy.",
    "Taker fees are paid on every fill, rounded up to the cent once per order; the unfilled "
    "remainder of an order is dropped.",
]


__all__ = [
    "COST_CONTROLS",
    "FILL_ASSUMPTIONS",
    "LEAKAGE_CONTROLS",
    "MANIFEST_FILE",
    "GitState",
    "Plan",
    "Progress",
    "RunManifest",
    "RunStatus",
    "RunUsage",
    "SlotInfo",
    "SystemPlan",
    "file_sha256",
    "git_details",
    "git_state",
    "hashes_digest",
    "read_manifest",
    "slot_info",
    "write_manifest",
]
