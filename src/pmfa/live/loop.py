"""One pass of the live modes, and the foreground loop that repeats it.

A pass (``run_once``), in order:

1. **Refresh the indexes.** The open events of every enabled series go into
   the events index; new documents (from a poll of the live sources, plus any
   published since the last look) go into the evidence index.
2. **Resolve outcomes** with ``pmfa.evaluation.resolver.resolve_outcomes``.
3. **Expire due proposals**: a pending proposal past its time, or whose price
   has passed its edge bound, ends its episode ``expired``.
4. **Apply the self-halt conditions** (``pmfa.execution.halt.enforce``), and
   demote a stage when the daily loss stop tripped on two days of the last seven
   (``StageManager.check_demotion``).
5. **Detect triggers** (``pmfa.live.triggers``) and run one episode per
   trigger. When the agent is halted the episodes still run and record their
   forecasts, and the pipeline refuses every order (exit ``halted``).
6. **Mark equity** in the state store, through the executor's own ``mark``:
   holdings at their own side's best bid, at cost without one. The paper
   executor marks at every fill and settlement the same way, so the equity
   series behind the daily loss stop and the drawdown halt has one definition.

The last look is kept in a small cursor file under the data directory
(``live-<mode>-cursor.json``), with each open event's last price-move trigger
that ran (the anchor its next move is measured from, see ``pmfa.live.triggers``). On the very first look there is no cursor:
the indexes are filled with everything visible, and no document trigger fires,
since documents published before the agent started watching are background,
not news.

``run_loop`` repeats passes in the foreground, ``interval_seconds`` apart on the
world's clock, until stopped (Ctrl-C), until ``max_passes``, or until the run's
dollar cap is reached. It is a loop a person starts and stops, not a service.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pmfa.errors import ExchangeError, NotFound, PmfaError, RunBudgetExhausted
from pmfa.live import triggers as trig
from pmfa.ports import World
from pmfa.records.base import Dollars, Record, RunMode, UtcTime
from pmfa.records.decision import OrderResult, Proposal
from pmfa.records.episode import EpisodeRecord, EpisodeStatus, Exit
from pmfa.records.evidence import Document, Trigger, TriggerKind
from pmfa.records.market import Event

if TYPE_CHECKING:
    from pmfa.episode import EpisodeController

Resolver = Callable[..., Any]
"""``resolve_outcomes(world, *, now=None)``."""


class EpisodeOutcome(Record):
    """What one trigger's episode decided, as the pass reports it."""

    trigger: Trigger
    episode_id: str
    event_ticker: str | None = None
    status: EpisodeStatus
    exit: Exit | None = None
    exit_detail: str = ""
    proposal: Proposal | None = None
    orders: list[OrderResult] = []
    already_run: bool = False
    """The episode was in the store before this pass: nothing ran again."""


class PassSummary(Record):
    """What one pass did."""

    mode: RunMode
    started_at: UtcTime
    finished_at: UtcTime
    first_look: bool
    open_events: list[str] = []
    events_indexed: int = 0
    documents_new: int = 0
    passages_indexed: int = 0
    resolved: list[str] = []
    """One line per resolution the resolver reported."""
    expired: list[str] = []
    """Proposal ids expired in this pass."""
    stop_reasons: list[str] = []
    halted: bool = False
    halt_reason: str = ""
    triggers: list[Trigger] = []
    episodes: list[EpisodeOutcome] = []
    pending_review: list[str] = []
    """Proposal ids waiting for a human after this pass."""
    equity: Dollars | None = None
    notes: list[str] = []
    stopped: str = ""
    """Set when the run's dollar cap was reached: the loop must stop."""


# --------------------------------------------------------------------------- the cursor


def cursor_path(world: World) -> Path:
    """The file that remembers the last look of this world's mode."""
    return Path(world.config.paths.data_dir) / f"live-{world.mode.value}-cursor.json"


def _read_cursor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_last_look(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(_read_cursor(path)["last_look"])
    except (ValueError, KeyError, TypeError):
        return None


def read_price_anchors(path: Path) -> dict[str, datetime]:
    """Each event's last price-move trigger that ran, as kept in the cursor file.

    Price moves are measured from these (``pmfa.live.triggers``), so the chain
    of crossings is the same from one pass to the next.
    """
    raw = _read_cursor(path).get("price_move_anchors")
    anchors: dict[str, datetime] = {}
    if isinstance(raw, dict):
        for event, at in raw.items():
            try:
                anchors[str(event)] = datetime.fromisoformat(at)
            except (ValueError, TypeError):
                continue
    return anchors


def write_last_look(
    path: Path, at: datetime, *, price_anchors: Mapping[str, datetime] | None = None
) -> None:
    """Save the last look; ``price_anchors`` replaces the stored anchors (``None`` keeps them)."""
    anchors = read_price_anchors(path) if price_anchors is None else dict(price_anchors)
    data: dict[str, Any] = {"last_look": at.isoformat()}
    if anchors:
        data["price_move_anchors"] = {e: t.isoformat() for e, t in sorted(anchors.items())}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- steps


def _open_events(world: World, notes: list[str]) -> list[Event]:
    events: list[Event] = []
    for series in world.config.follow.enabled_series():
        try:
            events.extend(world.exchange.list_events(series.ticker))
        except (ExchangeError, NotFound) as exc:
            notes.append(f"events of {series.ticker} not read: {exc}")
    return [e for e in events if e.markets]


def _index(knowledge: object, method: str, items: list[Any]) -> int:
    """Call an ingestion method when the knowledge port has it (the port itself is read-only)."""
    fn = getattr(knowledge, method, None)
    if fn is None or not items:
        return 0
    return int(fn(items))


def _new_documents(
    world: World, last_look: datetime | None, now: datetime, notes: list[str]
) -> list[Document]:
    """Documents new since the last look: polled into the corpus now, or published since then."""
    added: list[Document] = []
    poll = getattr(world.evidence, "poll", None)
    if callable(poll):
        added = list(poll(force=True))
        for failure in getattr(world.evidence, "last_failures", []) or []:
            notes.append(f"evidence not fetched: {failure}")
    visible = world.evidence.documents(as_of=now, published_after=last_look)
    unique = {d.doc_id: d for d in [*added, *visible] if d.published_at <= now}
    return sorted(unique.values(), key=lambda d: (d.published_at, d.doc_id))


def describe_resolution(result: Any) -> list[str]:
    """Plain lines for whatever the resolver returned (a record, a list of them, or a count)."""
    if result is None:
        return []
    if isinstance(result, Record):
        fields = result.model_dump(mode="json")
        items = [v for v in fields.values() if isinstance(v, list)]
        if len(items) == 1 and all(isinstance(x, dict | str) for x in items[0]):
            return [_one_line(x) for x in items[0]]
        return [_one_line(fields)]
    if isinstance(result, list | tuple):
        return [
            _one_line(x.model_dump(mode="json") if isinstance(x, Record) else x) for x in result
        ]
    return [str(result)]


def _one_line(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items() if not isinstance(v, list | dict))
    return str(value)


def default_resolver() -> Resolver:
    from pmfa.evaluation.resolver import resolve_outcomes

    return resolve_outcomes


def _resolve(world: World, resolver: Resolver | None, notes: list[str]) -> list[str]:
    try:
        fn = resolver if resolver is not None else default_resolver()
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("pmfa.evaluation"):
            notes.append("outcome resolver not available in this checkout: nothing resolved")
            return []
        raise
    try:
        return describe_resolution(fn(world, now=world.clock.now()))
    except (ExchangeError, NotFound) as exc:
        notes.append(f"outcome resolution failed: {exc}")
        return []


def _expire_due(world: World, controller: EpisodeController, notes: list[str]) -> list[str]:
    """End every pending proposal that is past its time or its edge bound as ``expired``."""
    from pmfa.episode import NotPaused
    from pmfa.execution.review import ReviewQueue

    queue = ReviewQueue(world.store, world.clock, world.config)
    expired = []
    for proposal in queue.pending():
        try:
            book = world.executor.execution_book(proposal.market_ticker)
        except (ExchangeError, NotFound):
            book = None
        if not queue.expired(proposal, book):
            continue
        try:
            controller.expire(proposal.episode_id)
        except (NotPaused, NotFound) as exc:
            queue.expire(proposal.proposal_id)
            notes.append(f"proposal {proposal.proposal_id} expired; its episode: {exc}")
        expired.append(proposal.proposal_id)
    return expired


def outcome(record: EpisodeRecord, trigger: Trigger, *, already_run: bool) -> EpisodeOutcome:
    return EpisodeOutcome(
        trigger=trigger,
        episode_id=record.episode_id,
        event_ticker=record.event_ticker,
        status=record.status,
        exit=record.exit,
        exit_detail=record.exit_detail,
        proposal=record.proposal,
        orders=record.orders,
        already_run=already_run,
    )


def mark_equity(world: World) -> Decimal:
    """Record a marked equity point now and return it.

    The executor's ``mark`` records the point itself (the paper executor into its
    store; the demo executor through its ``equity_sink``, which
    ``build_demo_world`` sets to the store). An executor without one has no
    marks, and its at-cost equity is recorded instead.
    """
    now = world.clock.now()
    mark = getattr(world.executor, "mark", None)
    if callable(mark):
        return Decimal(mark(now))
    equity = world.executor.balance().equity
    world.store.record_equity(now, equity)
    return equity


# --------------------------------------------------------------------------- the pass


def run_once(
    world: World, controller: EpisodeController, *, resolver: Resolver | None = None
) -> PassSummary:
    """One pass: refresh, resolve, expire, self-halt, detect triggers, run episodes, mark equity.

    ``resolver`` replaces ``pmfa.evaluation.resolver.resolve_outcomes`` (tests).
    """
    from pmfa.episode import episode_id_for
    from pmfa.execution.halt import enforce
    from pmfa.execution.stages import StageManager
    from pmfa.records.decision import ProposalStatus

    store, clock, config = world.store, world.clock, world.config
    started = clock.now()
    notes: list[str] = []
    path = cursor_path(world)
    last_look = read_last_look(path)

    events = _open_events(world, notes)
    events_indexed = _index(world.knowledge, "index_events", events)
    try:
        documents = _new_documents(world, last_look, started, notes)
    except PmfaError as exc:
        # An unreachable source must not skip the self-halt checks or the equity mark.
        notes.append(f"evidence not read: {exc}")
        documents = []
    passages = _index(world.knowledge, "index_documents", documents)

    resolved = _resolve(world, resolver, notes)
    expired = _expire_due(world, controller, notes)
    stops = enforce(store, world.executor, config, clock, world.evidence)
    demoted = StageManager(store, config, clock).check_demotion()
    if demoted is not None:
        notes.append(f"stage demoted to {demoted.stage.value}: {demoted.reason}")

    anchors = read_price_anchors(path)
    found = trig.combine(
        trig.document_triggers(documents, config) if last_look is not None else [],
        trig.price_move_triggers(
            world.exchange, events, config, started, notes=notes, anchors=anchors
        ),
        trig.schedule_triggers(
            world.executor, config, started, open_events=[e.event_ticker for e in events]
        ),
    )

    outcomes: list[EpisodeOutcome] = []
    stopped = ""
    for trigger in found:
        episode_id = episode_id_for(world.mode, trigger)
        already = store.get_episode(episode_id) is not None
        try:
            record = controller.run(trigger)
        except RunBudgetExhausted as exc:
            stopped = f"the run's dollar cap was reached: {exc}"
            recorded = store.get_episode(episode_id)
            if recorded is not None:
                outcomes.append(outcome(recorded, trigger, already_run=already))
            break
        outcomes.append(outcome(record, trigger, already_run=already))

    equity = None
    try:
        equity = mark_equity(world)
    except PmfaError as exc:
        notes.append(f"equity not marked: {exc}")
    open_now = {e.event_ticker for e in events}
    for o in outcomes:
        t = o.trigger
        if t.kind is TriggerKind.PRICE_MOVE and t.event_ticker is not None:
            anchors[t.event_ticker] = max(anchors.get(t.event_ticker, t.at), t.at)
    write_last_look(
        path, started, price_anchors={e: at for e, at in anchors.items() if e in open_now}
    )
    halt = store.get_halt()
    return PassSummary(
        mode=world.mode,
        started_at=started,
        finished_at=clock.now(),
        first_look=last_look is None,
        open_events=[e.event_ticker for e in events],
        events_indexed=events_indexed,
        documents_new=len(documents) if last_look is not None else 0,
        passages_indexed=passages,
        resolved=resolved,
        expired=expired,
        stop_reasons=stops,
        halted=halt.halted,
        halt_reason=halt.reason,
        triggers=found,
        episodes=outcomes,
        pending_review=[p.proposal_id for p, _ in store.list_proposals(ProposalStatus.PENDING)],
        equity=equity,
        notes=notes,
        stopped=stopped,
    )


def _failed_pass(world: World, started: datetime, exc: PmfaError) -> PassSummary:
    """The summary of a pass that stopped on an error: what failed, and the halt flag."""
    halt = world.store.get_halt()
    return PassSummary(
        mode=world.mode,
        started_at=started,
        finished_at=world.clock.now(),
        first_look=read_last_look(cursor_path(world)) is None,
        halted=halt.halted,
        halt_reason=halt.reason,
        notes=[f"pass failed: {type(exc).__name__}: {exc}"],
    )


def run_loop(
    world: World,
    controller: EpisodeController,
    *,
    interval_seconds: float,
    max_passes: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_pass: Callable[[PassSummary], None] | None = None,
    resolver: Resolver | None = None,
) -> int:
    """Run passes ``interval_seconds`` apart (start to start, on the world's clock).

    Stops after ``max_passes``, when a pass reports the run's dollar cap was
    reached, or when the person interrupts it (``KeyboardInterrupt`` propagates
    to the caller). A pass that fails with a project error (``PmfaError``: a
    source or the exchange unreachable) is reported as a summary with one
    ``pass failed`` note, and the loop goes on. ``on_pass`` sees each summary.
    Returns the passes run.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    interval = timedelta(seconds=interval_seconds)
    passes = 0
    while max_passes is None or passes < max_passes:
        start = world.clock.now()
        try:
            summary = run_once(world, controller, resolver=resolver)
        except PmfaError as exc:
            # One failed pass (a source or the exchange down) is reported; the next one runs.
            summary = _failed_pass(world, start, exc)
        passes += 1
        if on_pass is not None:
            on_pass(summary)
        if summary.stopped or (max_passes is not None and passes >= max_passes):
            break
        wait = (start + interval - world.clock.now()).total_seconds()
        if wait > 0:
            sleep(wait)
    return passes


__all__ = [
    "EpisodeOutcome",
    "PassSummary",
    "cursor_path",
    "default_resolver",
    "describe_resolution",
    "mark_equity",
    "read_last_look",
    "read_price_anchors",
    "run_loop",
    "run_once",
    "write_last_look",
]
