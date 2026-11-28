"""The recorded world: one JSON file that holds everything an episode can see.

Replay, the examples, and the episode tests run on it. A ``RecordedWorldFile``
is both the record and the on-disk format, and is kept small enough to read:
the trigger (whose ``at`` is the as-of time), the series and events with their
markets and quotes as of the trigger, the order books at the trigger, the books
the pipeline reads at execution time and, when they differ, the books orders
filled against, the documents and calendar, the starting state of the store,
configuration overrides, and an optional scripted human review.

The starting state is what the episode's decisions read from the store, as of
the as-of time and with nothing dated later: the stage and halt flag; the
ledger entries resolved by then (the ones calibration, the critic's examples
and the calibration stop read); the prior orders whose outcome was known by
then, in the order they were sent, each with its fills up to then; the
settlements and the equity points dated at or before it; the bankroll; the
critic rejections stored for the events the episode forecast; and, for an
event forecast before, the time of its latest forecast (a price-move trigger
reads it). A world built from the file answers ``balance()``, ``positions()``,
the risk state, and the daily loss, drawdown, fill-failure and calibration
stops as the recorded world did at that time. The source-change stops are not
carried: the file holds neither the prior episodes nor the content hashes the
store remembered for their documents, so those stops cannot hold in a built
world.

The ports built from it obey the as-of rule inside each call:

- ``RecordedExchange`` answers as of its clock: no event or strike that opens
  later, no result that settles later, no candle that ends later.
- ``RecordedEvidence`` never returns a document published after ``as_of``.
- The built world's ``Knowledge`` indexes every document, including those
  dated after the trigger: the as-of filter inside retrieval, not omission,
  must keep them out, and the tests check that it does.
- A file written by ``pmfa.world.recording.Recording`` holds only what its
  episode read, so it also carries the episode's ``Knowledge`` calls with their
  results, and ``RecordedKnowledge`` serves them back in order instead.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from pmfa.config import Config
from pmfa.errors import ConfigError, NotFound, PmfaError
from pmfa.ports import Models, World
from pmfa.records.base import ONE, Dollars, Record, RunMode, Stage, UtcTime, stable_id
from pmfa.records.decision import (
    OrderResult,
    ReasonCode,
    Review,
    ReviewDecision,
    ReviewPolicy,
)
from pmfa.records.episode import LedgerEntry
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
from pmfa.records.forecast import Forecast
from pmfa.records.market import (
    BookLevel,
    Candle,
    Event,
    Market,
    MarketStatus,
    OrderBook,
    Series,
    Side,
)
from pmfa.world.clocks import SimClock

if TYPE_CHECKING:
    from pmfa.execution.paper import PaperExecutor
    from pmfa.ports import Clock, Exchange, Knowledge, StateStore
    from pmfa.state.store import SqliteStore

PRIOR_EPISODE = "recorded-prior"
"""Episode id of the prior critic rejections and forecasts a recorded world's store starts with."""


class ScriptedReview(Record):
    """A human review recorded with the world: what the human decides, and when.

    The proposal id and decision time are only known once the episode pauses,
    so the file holds the decision alone; ``review_for`` makes the ``Review``.
    A review that arrives after the proposal's expiry is how a fixture
    expresses an expired proposal.
    """

    decision: ReviewDecision
    contracts: int | None = None
    reason_code: ReasonCode | None = None
    note: str = ""
    reviewer: str = "human"
    after_seconds: float = 60.0
    """Seconds after the pause at which the review arrives."""

    def review_for(self, proposal_id: str, decided_at: datetime) -> Review:
        return Review(
            proposal_id=proposal_id,
            decision=self.decision,
            contracts=self.contracts,
            reason_code=self.reason_code,
            note=self.note,
            reviewer=self.reviewer,
            decided_at=decided_at,
        )


class RecordedSettlement(Record):
    """A market that settled at or before the as-of time, so the paper account pays it out."""

    market_ticker: str
    result: Side
    settled_at: UtcTime


class RecordedRejection(Record):
    """A critic rejection stored for an event before the episode; the forecaster is shown it."""

    event_ticker: str
    claim: str
    reason: str


class RecordedEquityPoint(Record):
    """An equity point at or before the as-of time, as the daily loss stop and drawdown halt read it."""

    at: UtcTime
    equity: Dollars


KnowledgeMethod = Literal[
    "search_events", "search_evidence", "search_ledger", "get_passages", "origins"
]


class RecordedKnowledgeCall(Record):
    """One ``Knowledge`` call an episode made, with its arguments and what came back.

    A recorded file holds only the documents an episode touched, so an index
    rebuilt from them would not rank passages the way the full corpus did.
    Replaying the calls themselves keeps retrieval exact.
    """

    method: KnowledgeMethod
    arguments: dict[str, Any]
    """Plain JSON (see ``knowledge_arguments``): times as ISO strings, passages by id."""
    results: list[dict[str, Any]]
    """The returned records as plain JSON."""


class RecordedWorldFile(Record):
    """One recorded world, as a record and as its JSON file."""

    description: str
    provenance: Literal["hand-made fixture", "recorded"]
    trigger: Trigger
    """Its ``at`` is the as-of time."""
    series: list[Series]
    events: list[Event]
    """Events with their markets and quotes as of the trigger."""
    order_books: dict[str, OrderBook] = {}
    """The book per market at the trigger. A market without one gets a book
    synthesized from its quote (one level per side)."""
    execution_books: dict[str, OrderBook] = {}
    """What an order meets at execution time, per market. Absent: the trigger-time book.
    This is how a fixture says "the price moved before execution"."""
    fill_books: dict[str, OrderBook] = {}
    """What an order actually fills against, per market, when that differs from the execution
    book (a backtest fills against the candle after the as-of time plus the latency, which the
    decision never sees). Absent: the execution book."""
    liquidity: dict[str, dict[Side, Dollars]] = {}
    """Overrides for ``liquidity_dollars`` per market and side."""
    candles: dict[str, list[Candle]] = {}
    """Candles per market."""
    documents: list[Document] = []
    scheduled_events: list[ScheduledEvent] = []
    ledger: list[LedgerEntry] = []
    """Prior ledger entries; the resolved ones are indexed for the critic. A recording keeps
    only those resolved at or before the as-of time, the ones a decision can read."""
    orders: list[OrderResult] = []
    """Prior orders whose outcome was known at the as-of time, in the order they were sent, each
    with its fills. They give the holdings the world starts with and the order outcomes the
    fill-failure stop counts. A recording writes these."""
    settlements: list[RecordedSettlement] = []
    """Prior settlements at or before the as-of time, so a settled holding is paid out and closed."""
    equity: list[RecordedEquityPoint] = []
    """Equity points at or before the as-of time, for the daily loss stop and the drawdown halt.
    Empty: the built world gets one point at the trigger time, the account marked then."""
    rejections: list[RecordedRejection] = []
    """Critic rejections stored before the episode for the events it forecast, in the order they
    were made. The forecaster is shown them, so it does not propose the same claims again."""
    forecast_times: dict[str, UtcTime] = {}
    """Per event forecast before the episode, the as-of time of its latest pipeline forecast. A
    price-move trigger looks for evidence published after it."""
    bankroll: Dollars | None = None
    """The paper bankroll; ``config.execution.notional_bankroll`` when absent."""
    stage: Stage
    halted: bool = False
    config_overrides: dict[str, Any] = {}
    """Merged into the default configuration, section by section."""
    review: ScriptedReview | None = None
    mode: RunMode | None = None
    """The run mode the episode was recorded in. Replay runs in it, so the episode id and any
    mode-dependent rule match the recording. ``None``: replay mode."""
    knowledge_calls: list[RecordedKnowledgeCall] = []
    """Recorded ``Knowledge`` calls, served back in order by ``RecordedKnowledge``.
    Empty: the built world indexes ``documents``, ``events`` and ``ledger`` instead."""

    @property
    def as_of(self) -> datetime:
        return self.trigger.at


# --------------------------------------------------------------------------- file I/O


def load_recorded_world(path: str | Path) -> RecordedWorldFile:
    return RecordedWorldFile.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_recorded_world(file: RecordedWorldFile, path: str | Path) -> Path:
    """Write the file as indented JSON with defaults left out, so it stays short and diffable.

    An empty ``orders``, ``settlements`` or ``equity`` is a default too, so a file without
    them is written exactly as before those fields existed.
    """
    data = file.model_dump(mode="json", exclude_defaults=True)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def recorded_config(file: RecordedWorldFile) -> Config:
    """The default configuration with the file's overrides merged in (no ``pmfa.toml`` is read)."""
    try:
        return Config.model_validate(file.config_overrides)
    except ValidationError as exc:
        raise ConfigError(f"recorded world config_overrides: {exc}") from exc


# --------------------------------------------------------------------------- books


def _trigger_book(file: RecordedWorldFile, ticker: str) -> OrderBook:
    """The recorded trigger-time book, else one level per side from the market's quote."""
    recorded = file.order_books.get(ticker)
    if recorded is not None:
        return recorded
    market = _find_market(file, ticker)
    yes_bids: list[BookLevel] = []
    no_bids: list[BookLevel] = []
    if market.yes_bid is not None and market.yes_bid > 0:
        yes_bids.append(BookLevel(price=market.yes_bid, contracts=market.yes_bid_size or 0))
    if market.yes_ask is not None and market.yes_ask < ONE:
        no_bids.append(BookLevel(price=ONE - market.yes_ask, contracts=market.yes_ask_size or 0))
    return OrderBook(ticker=ticker, as_of=file.as_of, yes_bids=yes_bids, no_bids=no_bids)


def _find_market(file: RecordedWorldFile, ticker: str) -> Market:
    for event in file.events:
        for market in event.markets:
            if market.ticker == ticker:
                return market
    raise NotFound(f"recorded world has no market {ticker!r}")


class RecordedBooks:
    """The books of execution time.

    ``book`` is what the pipeline reads before sending: the recorded execution
    book, else the trigger's. ``fill_book`` is what an order fills against: the
    recorded fill book, else ``book``.
    """

    def __init__(self, file: RecordedWorldFile) -> None:
        self.file = file

    def book(self, ticker: str) -> OrderBook:
        recorded = self.file.execution_books.get(ticker)
        return recorded if recorded is not None else _trigger_book(self.file, ticker)

    def fill_book(self, ticker: str) -> OrderBook:
        recorded = self.file.fill_books.get(ticker)
        return recorded if recorded is not None else self.book(ticker)

    __call__ = book


# --------------------------------------------------------------------------- exchange


class RecordedExchange:
    """The ``Exchange`` port over a recorded file, as of ``clock``."""

    def __init__(self, file: RecordedWorldFile, clock: Clock, config: Config | None = None) -> None:
        self.file = file
        self.clock = clock
        self.config = config if config is not None else recorded_config(file)

    def get_series(self, series_ticker: str) -> Series:
        for s in self.file.series:
            if s.ticker == series_ticker:
                return s
        raise NotFound(f"recorded world has no series {series_ticker!r}")

    def list_events(self, series_ticker: str, *, include_closed: bool = False) -> list[Event]:
        now = self.clock.now()
        events = [
            event
            for e in self.file.events
            if e.series_ticker == series_ticker and (event := self._as_of(e)) is not None
        ]
        if not include_closed:
            events = [
                e
                for e in events
                if any(m.status is MarketStatus.OPEN and m.close_time > now for m in e.markets)
            ]
        return sorted(events, key=lambda e: (e.close_time, e.event_ticker))

    def get_event(self, event_ticker: str) -> Event:
        for e in self.file.events:
            if e.event_ticker == event_ticker:
                event = self._as_of(e)
                if event is None:
                    break
                return event
        raise NotFound(f"recorded world has no event {event_ticker!r} as of {self.clock.now()}")

    def get_market(self, ticker: str) -> Market:
        market = _find_market(self.file, ticker)
        for listed in self.get_event(market.event_ticker).markets:  # raises if not open yet
            if listed.ticker == ticker:
                return listed
        raise NotFound(f"recorded market {ticker!r} has not opened as of {self.clock.now()}")

    def get_order_book(self, ticker: str) -> OrderBook:
        self.get_market(ticker)
        return _trigger_book(self.file, ticker)

    def get_candles(
        self, ticker: str, *, start: datetime, end: datetime, interval_minutes: int
    ) -> list[Candle]:
        end = min(end, self.clock.now())
        return sorted(
            (
                c
                for c in self.file.candles.get(ticker, [])
                if c.interval_minutes == interval_minutes and start < c.end_ts <= end
            ),
            key=lambda c: c.end_ts,
        )

    def liquidity_dollars(self, ticker: str, side: Side) -> Decimal:
        self.get_market(ticker)  # raises NotFound for a market not open by the clock's time
        override = self.file.liquidity.get(ticker, {}).get(side)
        if override is not None:
            return override
        book = self.get_order_book(ticker)
        return book.resting_dollars_near_touch(side, self.config.caps.liquidity_band)

    def _as_of(self, event: Event) -> Event | None:
        """The event with only the markets open by the clock's time; ``None`` before any opens."""
        now = self.clock.now()
        listed = [self._market_as_of(m) for m in event.markets if m.open_time <= now]
        return event.model_copy(update={"markets": listed}) if listed else None

    def _market_as_of(self, market: Market) -> Market:
        """Hide a result that settles after the clock's time."""
        if market.result is None:
            return market
        if market.settled_at is not None and market.settled_at <= self.clock.now():
            return market
        status = MarketStatus.CLOSED if market.status is MarketStatus.SETTLED else market.status
        return market.model_copy(update={"result": None, "settled_at": None, "status": status})


# --------------------------------------------------------------------------- evidence


class RecordedEvidence:
    """The ``Evidence`` port over a recorded file. The as-of rule is applied in every call."""

    def __init__(self, file: RecordedWorldFile) -> None:
        self.file = file

    def documents(
        self, *, as_of: datetime, published_after: datetime | None = None
    ) -> list[Document]:
        docs = [
            d
            for d in self.file.documents
            if d.published_at <= as_of
            and (published_after is None or d.published_at > published_after)
        ]
        return sorted(docs, key=lambda d: (d.published_at, d.doc_id))

    def get_document(self, doc_id: str, *, as_of: datetime) -> Document:
        for d in self.file.documents:
            if d.doc_id == doc_id and d.published_at <= as_of:
                return d
        raise NotFound(f"no document {doc_id!r} published by {as_of}")

    def scheduled_events(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        return sorted(
            (s for s in self.file.scheduled_events if start < s.scheduled_at <= end),
            key=lambda s: (s.scheduled_at, s.scheduled_event_id),
        )


# --------------------------------------------------------------------------- knowledge


class RecordedKnowledgeMiss(PmfaError):
    """Replay made a ``Knowledge`` call that was not recorded, or with different arguments.

    Like a cassette miss, it means the replay is no longer faithful, so it stops
    the episode instead of returning something made up.
    """


KNOWLEDGE_RESULTS: dict[str, type[Record]] = {
    "search_events": EventHit,
    "search_evidence": RetrievedPassage,
    "search_ledger": LedgerHit,
    "get_passages": Passage,
    "origins": Origin,
}
"""The record type each ``Knowledge`` method returns a list of."""


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def knowledge_arguments(method: str, **arguments: Any) -> dict[str, Any]:
    """A ``Knowledge`` call's arguments as plain JSON, the form they are recorded and compared in.

    ``origins`` takes passages; they are recorded by id, since the passages
    themselves are already in the recorded search results.
    """
    if method == "origins":
        passages = arguments.pop("passages")
        arguments = {"passage_ids": [p.passage_id for p in passages], **arguments}
    return {name: _plain(value) for name, value in arguments.items()}


class RecordedKnowledge:
    """The ``Knowledge`` port answered from recorded calls, in the order they were made.

    Each call must match the next recorded one: the same method and the same
    arguments. Anything else raises ``RecordedKnowledgeMiss`` naming the first
    difference. The as-of rule was applied when the calls were recorded.
    """

    def __init__(self, calls: list[RecordedKnowledgeCall]) -> None:
        self.calls = list(calls)
        self.position = 0
        self._lock = threading.Lock()

    def _serve(self, method: str, **arguments: Any) -> list[Any]:
        from pmfa.models.cassette import first_difference

        now = knowledge_arguments(method, **arguments)
        with self._lock:
            n = self.position + 1
            if self.position >= len(self.calls):
                raise RecordedKnowledgeMiss(
                    f"knowledge call #{n} ({method}) was not recorded: the file holds "
                    f"{len(self.calls)} call(s)"
                )
            recorded = self.calls[self.position]
            if recorded.method != method:
                raise RecordedKnowledgeMiss(
                    f"knowledge call #{n} asks for {method}, the file recorded {recorded.method}"
                )
            difference = first_difference(recorded.arguments, now)
            if difference is not None:
                raise RecordedKnowledgeMiss(
                    f"knowledge call #{n} ({method}) arguments differ from the recording at "
                    f"{difference}"
                )
            self.position += 1
        result_type = KNOWLEDGE_RESULTS[method]
        return [result_type.model_validate(r) for r in recorded.results]

    def remaining(self) -> int:
        """Recorded calls not yet served (a faithful replay ends at 0)."""
        with self._lock:
            return len(self.calls) - self.position

    def search_events(self, query: str, *, as_of: datetime, k: int = 5) -> list[EventHit]:
        return self._serve("search_events", query=query, as_of=as_of, k=k)

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        return self._serve(
            "search_evidence",
            queries=queries,
            as_of=as_of,
            category=category,
            k=k,
            published_after=published_after,
        )

    def search_ledger(
        self, query: str, *, as_of: datetime, category: str, k: int = 5
    ) -> list[LedgerHit]:
        return self._serve("search_ledger", query=query, as_of=as_of, category=category, k=k)

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        return self._serve("get_passages", passage_ids=passage_ids, as_of=as_of)

    def origins(self, passages: list[Passage], *, category: str) -> list[Origin]:
        return self._serve("origins", passages=passages, category=category)


# --------------------------------------------------------------------------- the world


@dataclass(frozen=True)
class RecordedWorld(World):
    """A ``World`` built from a recorded file. ``clock`` is a ``SimClock`` tests can advance."""

    file: RecordedWorldFile


def _seeded_store(file: RecordedWorldFile) -> SqliteStore:
    """A fresh in-memory store holding the file's starting state (see ``build_recorded_world``)."""
    from pmfa.state.store import MEMORY, SqliteStore

    at = file.as_of
    store = SqliteStore(MEMORY)
    store.set_stage(file.stage, "recorded world", at)
    if file.halted:
        store.set_halt("recorded world starts halted", at)
    if file.ledger:
        store.add_ledger_entries(list(file.ledger))
    for order in file.orders:
        store.save_order(order)
    for s in file.settlements:
        store.record_settlement(s.market_ticker, s.result, s.settled_at)
    for point in file.equity:
        store.record_equity(point.at, point.equity)
    for r in file.rejections:
        store.record_rejection(PRIOR_EPISODE, r.event_ticker, r.claim, r.reason)
    for event_ticker, forecast_at in sorted(file.forecast_times.items()):
        store.save_forecast(_prior_forecast(event_ticker, forecast_at))
    return store


def _prior_forecast(event_ticker: str, at: datetime) -> Forecast:
    """A stand-in for an event's latest prior forecast: only its time is ever read back."""
    return Forecast(
        forecast_id=stable_id("recorded-forecast", event_ticker, at.isoformat()),
        episode_id=PRIOR_EPISODE,
        event_ticker=event_ticker,
        as_of=at,
        outcome_probabilities={},
        market_probabilities={},
        coverage=0.0,
        confidence=0.0,
        n_independent_origins=0,
    )


def _paper_executor(
    file: RecordedWorldFile, store: StateStore, clock: Clock, config: Config, exchange: Exchange
) -> PaperExecutor:
    from pmfa.execution.paper import PaperExecutor

    books = RecordedBooks(file)
    return PaperExecutor(
        store,
        clock,
        config,
        exchange,
        books,
        bankroll=file.bankroll,
        fill_books=books.fill_book,
    )


def build_recorded_world(
    file: RecordedWorldFile,
    *,
    models: Models,
    review_policy: ReviewPolicy,
    mode: RunMode = RunMode.REPLAY,
) -> RecordedWorld:
    """Every port from one recorded file, with a fresh in-memory store and index.

    The store is seeded with the stage, the halt flag, the prior ledger entries,
    the prior orders (in the file's order), the settlements, the equity points,
    the critic rejections and a stand-in forecast per entry of ``forecast_times``.
    Only when the file gives no equity point does it get one at the trigger time,
    the account marked then.

    The knowledge index holds every document (future ones included: retrieval's
    as-of filter must keep them out), every event, and the resolved ledger
    entries, embedded with the deterministic hashing embedder. A file with
    recorded knowledge calls gets ``RecordedKnowledge`` instead, and no index.
    """
    config = recorded_config(file)
    clock = SimClock(file.as_of)
    at = file.as_of
    store = _seeded_store(file)

    knowledge: Knowledge
    if file.knowledge_calls:
        knowledge = RecordedKnowledge(list(file.knowledge_calls))
    else:
        from pmfa.knowledge.embedders import HashingEmbedder
        from pmfa.knowledge.retriever import ChromaKnowledge

        index = ChromaKnowledge(config, embedder=HashingEmbedder())
        index.index_documents(list(file.documents))
        index.index_events([e for e in file.events if e.markets])
        index.index_ledger(list(file.ledger))
        knowledge = index

    exchange = RecordedExchange(file, clock, config)
    executor = _paper_executor(file, store, clock, config, exchange)
    if not file.equity:
        executor.mark(at)

    return RecordedWorld(
        mode=mode,
        config=config,
        clock=clock,
        exchange=exchange,
        executor=executor,
        evidence=RecordedEvidence(file),
        knowledge=knowledge,
        models=models,
        store=store,
        review_policy=review_policy,
        file=file,
    )


__all__ = [
    "PRIOR_EPISODE",
    "RecordedBooks",
    "RecordedEquityPoint",
    "RecordedEvidence",
    "RecordedExchange",
    "RecordedKnowledge",
    "RecordedKnowledgeCall",
    "RecordedKnowledgeMiss",
    "RecordedRejection",
    "RecordedSettlement",
    "RecordedWorld",
    "RecordedWorldFile",
    "ScriptedReview",
    "build_recorded_world",
    "knowledge_arguments",
    "load_recorded_world",
    "recorded_config",
    "save_recorded_world",
]
