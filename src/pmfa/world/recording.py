"""Recording: the other half of record and replay.

``Recording(world)`` wraps a world's exchange, executor, evidence and knowledge
ports so that everything an episode reads is captured as it is read: series,
events, markets, order books, candles and liquidity from the exchange; the
books orders meet from the executor; documents and calendar entries from the
evidence port; and every ``Knowledge`` call with its results. Run the episode
on ``recording.world``; then ``recording.file(trigger, ...)`` returns a
``RecordedWorldFile`` holding exactly what was read, and
``build_recorded_world`` on that file replays the episode without the
original world (no snapshot, corpus, index, network or key).

What a recorded file can and cannot say:

- Exchange reads keep the first answer seen per market. A recorded exchange is
  static, so a world whose quotes changed during the episode is recorded as it
  stood at the first read. The book an order meets is recorded separately
  (``execution_books``), first answer per market, which is how a price move
  before execution survives.
- The starting state is read from the store when the recording starts: stage,
  halt flag, prior ledger entries, prior fills, and the bankroll. Earlier
  equity points are not carried over.
- The configuration is recorded as its differences from the defaults.

``save_example`` writes a recorded file, its cassette, and the episode's
outputs as an example directory (see ``examples/README.md``).
"""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pmfa.config import Config
from pmfa.models.cassette import Cassette
from pmfa.ports import Evidence, Exchange, Executor, Knowledge, World
from pmfa.records.decision import OrderIntent, OrderResult, ReviewCard
from pmfa.records.episode import EpisodeRecord
from pmfa.records.evidence import (
    Document,
    EventHit,
    LedgerHit,
    Origin,
    Passage,
    RetrievedPassage,
    ScheduledEvent,
    Trigger,
)
from pmfa.records.market import (
    Balance,
    Candle,
    Event,
    Market,
    OrderBook,
    Position,
    Series,
    Side,
)
from pmfa.world.recorded import (
    RecordedKnowledgeCall,
    RecordedWorldFile,
    knowledge_arguments,
    save_recorded_world,
)

# --------------------------------------------------------------------------- the ports


class _RecordingExchange:
    """The ``Exchange`` port, passing every read through and remembering the answer."""

    def __init__(self, recording: Recording, inner: Exchange) -> None:
        self._r = recording
        self._inner = inner

    def get_series(self, series_ticker: str) -> Series:
        series = self._inner.get_series(series_ticker)
        self._r._add_series(series)
        return series

    def list_events(self, series_ticker: str, *, include_closed: bool = False) -> list[Event]:
        events = self._inner.list_events(series_ticker, include_closed=include_closed)
        for event in events:
            self._r._add_event(event)
        return events

    def get_event(self, event_ticker: str) -> Event:
        event = self._inner.get_event(event_ticker)
        self._r._add_event(event)
        return event

    def get_market(self, ticker: str) -> Market:
        market = self._inner.get_market(ticker)
        if market.event_ticker not in self._r._events:
            # A recorded market lives inside its event, so the event is read too.
            self._r._add_event(self._inner.get_event(market.event_ticker))
        return market

    def get_order_book(self, ticker: str) -> OrderBook:
        book = self._inner.get_order_book(ticker)
        with self._r._lock:
            self._r._books.setdefault(ticker, book)
        return book

    def get_candles(
        self, ticker: str, *, start: datetime, end: datetime, interval_minutes: int
    ) -> list[Candle]:
        candles = self._inner.get_candles(
            ticker, start=start, end=end, interval_minutes=interval_minutes
        )
        with self._r._lock:
            held = self._r._candles.setdefault(ticker, {})
            for c in candles:
                held.setdefault((c.interval_minutes, c.end_ts), c)
        return candles

    def liquidity_dollars(self, ticker: str, side: Side) -> Decimal:
        dollars = self._inner.liquidity_dollars(ticker, side)
        with self._r._lock:
            self._r._liquidity.setdefault(ticker, {}).setdefault(side, dollars)
        return dollars

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecordingExecutor:
    """The ``Executor`` port: orders pass through; the books orders meet are remembered."""

    def __init__(self, recording: Recording, inner: Executor) -> None:
        self._r = recording
        self._inner = inner
        self.simulated = inner.simulated

    def balance(self) -> Balance:
        return self._inner.balance()

    def positions(self, event_ticker: str | None = None) -> list[Position]:
        return self._inner.positions(event_ticker)

    def execution_book(self, ticker: str) -> OrderBook:
        book = self._inner.execution_book(ticker)
        with self._r._lock:
            self._r._execution_books.setdefault(ticker, book)
        return book

    def place(self, intent: OrderIntent) -> OrderResult:
        return self._inner.place(intent)

    def cancel_all(self) -> int:
        return self._inner.cancel_all()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecordingEvidence:
    """The ``Evidence`` port, remembering every document and calendar entry returned."""

    def __init__(self, recording: Recording, inner: Evidence) -> None:
        self._r = recording
        self._inner = inner

    def documents(
        self, *, as_of: datetime, published_after: datetime | None = None
    ) -> list[Document]:
        docs = self._inner.documents(as_of=as_of, published_after=published_after)
        for doc in docs:
            self._r._add_document(doc)
        return docs

    def get_document(self, doc_id: str, *, as_of: datetime) -> Document:
        doc = self._inner.get_document(doc_id, as_of=as_of)
        self._r._add_document(doc)
        return doc

    def scheduled_events(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        entries = self._inner.scheduled_events(start=start, end=end)
        with self._r._lock:
            for s in entries:
                self._r._scheduled.setdefault(s.scheduled_event_id, s)
        return entries

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecordingKnowledge:
    """The ``Knowledge`` port, recording each call's arguments and results in order."""

    def __init__(self, recording: Recording, inner: Knowledge) -> None:
        self._r = recording
        self._inner = inner

    def _record(self, method: str, results: list[Any], **arguments: Any) -> None:
        call = RecordedKnowledgeCall(
            method=method,  # type: ignore[arg-type]
            arguments=knowledge_arguments(method, **arguments),
            results=[r.model_dump(mode="json") for r in results],
        )
        with self._r._lock:
            self._r._knowledge.append(call)

    def search_events(self, query: str, *, as_of: datetime, k: int = 5) -> list[EventHit]:
        hits = self._inner.search_events(query, as_of=as_of, k=k)
        self._record("search_events", hits, query=query, as_of=as_of, k=k)
        return hits

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        hits = self._inner.search_evidence(
            queries, as_of=as_of, category=category, k=k, published_after=published_after
        )
        self._record(
            "search_evidence",
            hits,
            queries=list(queries),
            as_of=as_of,
            category=category,
            k=k,
            published_after=published_after,
        )
        return hits

    def search_ledger(
        self, query: str, *, as_of: datetime, category: str, k: int = 5
    ) -> list[LedgerHit]:
        hits = self._inner.search_ledger(query, as_of=as_of, category=category, k=k)
        self._record("search_ledger", hits, query=query, as_of=as_of, category=category, k=k)
        return hits

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        passages = self._inner.get_passages(passage_ids, as_of=as_of)
        self._record("get_passages", passages, passage_ids=list(passage_ids), as_of=as_of)
        return passages

    def origins(self, passages: list[Passage], *, category: str) -> list[Origin]:
        found = self._inner.origins(passages, category=category)
        self._record("origins", found, passages=list(passages), category=category)
        return found

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# --------------------------------------------------------------------------- the recording


def config_overrides(config: Config) -> dict[str, Any]:
    """``config`` as its differences from the defaults: ``{section: {field: value}}``, plain JSON."""
    default = Config().model_dump(mode="json")
    current = config.model_dump(mode="json")
    overrides: dict[str, Any] = {}
    for section, fields in current.items():
        changed = {k: v for k, v in fields.items() if default[section].get(k) != v}
        if changed:
            overrides[section] = changed
    return overrides


class Recording:
    """Wraps a world so that everything an episode reads is captured.

    Run the episode on ``recording.world``; then ``file(trigger, ...)`` gives the
    recorded world. One recording is meant for one episode (and its review).
    """

    def __init__(self, world: World) -> None:
        self.inner = world
        self._lock = threading.RLock()
        self._series: dict[str, Series] = {}
        self._events: dict[str, Event] = {}
        self._books: dict[str, OrderBook] = {}
        self._execution_books: dict[str, OrderBook] = {}
        self._liquidity: dict[str, dict[Side, Decimal]] = {}
        self._candles: dict[str, dict[tuple[int, datetime], Candle]] = {}
        self._documents: dict[str, Document] = {}
        self._scheduled: dict[str, ScheduledEvent] = {}
        self._knowledge: list[RecordedKnowledgeCall] = []

        # The starting state, read before anything runs.
        store = world.store
        self._stage = store.get_stage().stage
        self._halted = store.get_halt().halted
        self._ledger = store.ledger()
        self._fills = store.list_fills()
        bankroll = getattr(world.executor, "bankroll", None)
        self._bankroll: Decimal = (
            Decimal(bankroll) if bankroll is not None else world.executor.balance().equity
        )

        self.world: World = dataclasses.replace(
            world,
            exchange=_RecordingExchange(self, world.exchange),
            executor=_RecordingExecutor(self, world.executor),
            evidence=_RecordingEvidence(self, world.evidence),
            knowledge=_RecordingKnowledge(self, world.knowledge),
        )

    # -- capture

    def _add_series(self, series: Series) -> None:
        with self._lock:
            self._series.setdefault(series.ticker, series)

    def _add_event(self, event: Event) -> None:
        with self._lock:
            self._events.setdefault(event.event_ticker, event)

    def _add_document(self, doc: Document) -> None:
        with self._lock:
            self._documents.setdefault(doc.doc_id, doc)

    # -- the file

    def file(
        self,
        trigger: Trigger,
        *,
        description: str,
        provenance: Literal["hand-made fixture", "recorded"] = "recorded",
    ) -> RecordedWorldFile:
        """Exactly what the episode read, as a recorded world whose as-of time is ``trigger.at``."""
        with self._lock:
            series = dict(self._series)
            for event in self._events.values():
                if event.series_ticker not in series:
                    series[event.series_ticker] = self.inner.exchange.get_series(
                        event.series_ticker
                    )
            return RecordedWorldFile(
                description=description,
                provenance=provenance,
                trigger=trigger,
                series=sorted(series.values(), key=lambda s: s.ticker),
                events=sorted(self._events.values(), key=lambda e: e.event_ticker),
                order_books=dict(sorted(self._books.items())),
                execution_books=dict(sorted(self._execution_books.items())),
                liquidity={
                    t: dict(sorted(sides.items())) for t, sides in sorted(self._liquidity.items())
                },
                candles={
                    t: [held[k] for k in sorted(held)] for t, held in sorted(self._candles.items())
                },
                documents=sorted(
                    self._documents.values(), key=lambda d: (d.published_at, d.doc_id)
                ),
                scheduled_events=sorted(
                    self._scheduled.values(), key=lambda s: (s.scheduled_at, s.scheduled_event_id)
                ),
                ledger=list(self._ledger),
                fills=list(self._fills),
                bankroll=self._bankroll,
                stage=self._stage,
                halted=self._halted,
                config_overrides=config_overrides(self.inner.config),
                knowledge_calls=list(self._knowledge),
            )


# --------------------------------------------------------------------------- example directories

WORLD_FILE = "world.json"
CASSETTE_FILE = "cassette.json"
EXPECTED_DIR = "expected"
EPISODE_FILE = "episode.json"
CARD_FILE = "review_card.json"
README_FILE = "README.md"


def write_json(path: Path, data: Any) -> Path:
    """Indented JSON with a trailing newline: stable, diffable output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def save_example(
    directory: str | Path,
    file: RecordedWorldFile,
    cassette: Cassette,
    episode: EpisodeRecord,
    card: ReviewCard | None = None,
) -> Path:
    """Write an example directory: inputs, the cassette that replays them, and the outputs.

    ``world.json`` (the recorded world), ``cassette.json`` (the model records),
    ``expected/episode.json`` (the episode record: trace, forecast, decision,
    orders) and ``expected/review_card.json`` when a human review happened. A
    short ``README.md`` is written only when the directory has none, so a
    hand-written one is never overwritten.
    """
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    save_recorded_world(file, folder / WORLD_FILE)
    write_json(folder / CASSETTE_FILE, cassette.model_dump(mode="json"))
    expected = folder / EXPECTED_DIR
    write_json(expected / EPISODE_FILE, episode.model_dump(mode="json"))
    card_path = expected / CARD_FILE
    if card is not None:
        write_json(card_path, card.model_dump(mode="json"))
    elif card_path.exists():
        card_path.unlink()
    readme = folder / README_FILE
    if not readme.exists():
        exit_name = episode.exit.value if episode.exit else episode.status.value
        readme.write_text(
            f"# {folder.name}\n\n"
            f"{file.description}\n\n"
            f"- Provenance: {file.provenance}.\n"
            f"- Trigger: {file.trigger.kind.value} at {file.as_of.isoformat()}.\n"
            f"- Event: {episode.event_ticker or 'none matched'}. Exit: `{exit_name}`.\n\n"
            f"Replay it offline: `pmfa replay {folder.as_posix()}`.\n",
            encoding="utf-8",
        )
    return folder


__all__ = [
    "CARD_FILE",
    "CASSETTE_FILE",
    "EPISODE_FILE",
    "EXPECTED_DIR",
    "README_FILE",
    "WORLD_FILE",
    "Recording",
    "config_overrides",
    "save_example",
    "write_json",
]
