"""Ports: the only ways an episode touches the outside.

A *world* is one implementation of every port. A *run mode* is a choice of
world. The episode pipeline is the same code in every mode; only what sits
behind these protocols differs:

    mode      exchange                 evidence          models          executor
    replay    recorded snapshot        recorded corpus   cassettes       paper
    backtest  frozen snapshot, as-of   frozen corpus     live, cached    paper
    shadow    live production (read)   live feeds        live            paper, live book
    demo      Kalshi demo              live feeds        live            demo exchange

Rules every implementation follows:

- **As-of.** Nothing published or priced after the as-of time may be returned.
  ``Exchange`` reads are as of the world's clock. ``Evidence`` and ``Knowledge``
  take ``as_of`` as a required keyword and filter inside the call.
- **Read-only production.** The production exchange adapter implements
  ``Exchange`` only. It has no order methods and loads no credentials.
- **Record-only models.** ``Models.call`` takes an input record and returns a
  validated output record. No role can call a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pmfa.records.base import Record, RunMode, Stage
from pmfa.records.decision import (
    Fill,
    OrderIntent,
    OrderResult,
    Proposal,
    ProposalStatus,
    Review,
    ReviewCard,
    ReviewPolicy,
)
from pmfa.records.episode import (
    Calibration,
    EpisodeRecord,
    EpisodeStatus,
    HaltState,
    LedgerEntry,
    ModelResult,
    StageState,
    TraceStep,
    Usage,
)
from pmfa.records.evidence import (
    Document,
    EventHit,
    LedgerHit,
    Origin,
    Passage,
    RetrievedPassage,
    ScheduledEvent,
)
from pmfa.records.forecast import Forecast, TreeNode
from pmfa.records.market import Balance, Candle, Event, Market, OrderBook, Position, Series, Side
from pmfa.records.roles import ModelSlot, RejectionBrief, RoleTask

if TYPE_CHECKING:
    from pmfa.config import Config


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware UTC."""
        ...


@runtime_checkable
class Exchange(Protocol):
    """Read-only market data, as of the world's clock."""

    def get_series(self, series_ticker: str) -> Series: ...

    def list_events(self, series_ticker: str, *, include_closed: bool = False) -> list[Event]:
        """Events of a series with their markets. Open events only unless asked."""
        ...

    def get_event(self, event_ticker: str) -> Event:
        """One event with its markets, their rules, strikes, and quotes."""
        ...

    def get_market(self, ticker: str) -> Market: ...

    def get_order_book(self, ticker: str) -> OrderBook:
        """The book now. Snapshot worlds synthesize one level per side from the candle at the clock's time."""
        ...

    def get_candles(
        self, ticker: str, *, start: datetime, end: datetime, interval_minutes: int
    ) -> list[Candle]:
        """Candles whose period ends in ``(start, end]``. ``end`` is clamped to the clock's time."""
        ...

    def liquidity_dollars(self, ticker: str, side: Side) -> Decimal:
        """Dollars resting within the configured band of the touch on ``side``.

        Snapshot worlds have no depth data and return a candle-volume proxy.
        """
        ...


@runtime_checkable
class Executor(Protocol):
    """Places immediate-or-cancel limit orders and reports holdings."""

    simulated: bool
    """True for the paper executor. False only for the demo exchange."""

    def balance(self) -> Balance: ...

    def positions(self, event_ticker: str | None = None) -> list[Position]: ...

    def execution_book(self, ticker: str) -> OrderBook:
        """The book the pipeline decides with at execution time.

        Live modes: the current book. Replay: the recorded execution-time book.
        Backtest: the as-of book, so that no decision reads a price from after the
        as-of time. What an order then fills against is the executor's own business:
        in a backtest it is the first one-minute candle after the as-of time plus the
        configured latency, on the conservative side, capped at a share of its volume.
        """
        ...

    def place(self, intent: OrderIntent) -> OrderResult:
        """Send one immediate-or-cancel limit order. The unfilled remainder is gone."""
        ...

    def find_order(self, intent: OrderIntent) -> OrderResult | None:
        """The order already placed for this intent's client order id, if there is one.

        An episode uses it to reconcile after a crash or an exchange error, including
        one whose order outcome is unknown, which asks again until it can be answered.
        ``None`` means the exchange answered and lists no such order; raising
        ``ExchangeError`` means it could not be asked. It never places anything.
        """
        ...

    def cancel_all(self) -> int:
        """Cancel every open order. Returns how many were cancelled."""
        ...


@runtime_checkable
class Evidence(Protocol):
    """Documents published at or before a time, and the release calendar."""

    def documents(
        self, *, as_of: datetime, published_after: datetime | None = None
    ) -> list[Document]:
        """Documents with ``published_after < published_at <= as_of``, oldest first."""
        ...

    def get_document(self, doc_id: str, *, as_of: datetime) -> Document:
        """Raises ``NotFound`` if the document does not exist or was published after ``as_of``."""
        ...

    def scheduled_events(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        """Calendar entries with ``start < scheduled_at <= end``, earliest first."""
        ...


@runtime_checkable
class Knowledge(Protocol):
    """The three retrieval indexes. ``as_of`` is enforced inside every call."""

    def search_events(self, query: str, *, as_of: datetime, k: int = 5) -> list[EventHit]:
        """Events open at ``as_of``, by cosine similarity to ``query``."""
        ...

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        """Top passages over all queries: duplicates merged, at most three per origin, ranked."""
        ...

    def search_ledger(
        self, query: str, *, as_of: datetime, category: str, k: int = 5
    ) -> list[LedgerHit]:
        """Forecasts resolved before ``as_of``, same category first."""
        ...

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        """Passages by id. Ids that do not exist or postdate ``as_of`` are left out."""
        ...

    def origins(self, passages: list[Passage], *, category: str) -> list[Origin]:
        """The independent origins behind a set of passages, primary ones flagged for ``category``."""
        ...


@runtime_checkable
class Models(Protocol):
    """The model gateway: role and input record in, validated record out."""

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        """Run one role call.

        Raises ``BudgetExhausted`` instead of calling when an episode cap is
        reached, ``RunBudgetExhausted`` when the run's dollar cap is reached,
        and ``ModelOutputError`` when the answer and its one repair retry both
        fail validation or the model refuses.
        """
        ...

    def usage(self, episode_id: str) -> Usage: ...


class StateStore(Protocol):
    """The durable state: one SQLite database. The MCP state server wraps the same methods."""

    # -- episodes and traces
    def save_episode(self, episode: EpisodeRecord) -> None:
        """Insert or replace an episode (its trace steps are stored through ``append_trace``)."""
        ...

    def get_episode(self, episode_id: str) -> EpisodeRecord | None:
        """The episode with its full ordered trace."""
        ...

    def list_episodes(
        self,
        *,
        status: EpisodeStatus | None = None,
        event_ticker: str | None = None,
        limit: int = 100,
    ) -> list[EpisodeRecord]: ...

    def append_trace(self, episode_id: str, step: TraceStep) -> None: ...

    def get_trace(self, episode_id: str) -> list[TraceStep]: ...

    # -- scenario tree
    def put_node(self, node: TreeNode) -> None: ...

    def get_nodes(self, episode_id: str) -> list[TreeNode]: ...

    def record_rejection(
        self, episode_id: str, event_ticker: str, claim: str, reason: str
    ) -> None: ...

    def get_rejections(self, event_ticker: str) -> list[RejectionBrief]: ...

    # -- forecasts, proposals, reviews
    def save_forecast(self, forecast: Forecast) -> None: ...

    def last_forecast_time(self, event_ticker: str) -> datetime | None:
        """When this event was last forecast; evidence newer than this is "new"."""
        ...

    def save_proposal(
        self, proposal: Proposal, status: ProposalStatus, card: ReviewCard | None = None
    ) -> None: ...

    def get_proposal(
        self, proposal_id: str
    ) -> tuple[Proposal, ProposalStatus, ReviewCard | None] | None: ...

    def list_proposals(
        self, status: ProposalStatus | None = None
    ) -> list[tuple[Proposal, ProposalStatus]]: ...

    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None: ...

    def save_review(self, review: Review) -> None: ...

    def list_reviews(self) -> list[Review]: ...

    # -- orders, fills, settlements (the paper portfolio is derived from these)
    def save_order(self, order: OrderResult) -> None: ...

    def list_orders(self, *, episode_id: str | None = None) -> list[OrderResult]: ...

    def list_fills(self, *, market_ticker: str | None = None) -> list[Fill]: ...

    def record_settlement(self, market_ticker: str, result: Side, settled_at: datetime) -> None: ...

    def list_settlements(self) -> list[tuple[str, Side, datetime]]: ...

    def record_equity(self, at: datetime, equity: Decimal) -> None: ...

    def equity_series(self) -> list[tuple[datetime, Decimal]]: ...

    # -- ledger and calibration
    def add_ledger_entries(self, entries: list[LedgerEntry]) -> None: ...

    def resolve_market(self, market_ticker: str, outcome: bool, resolved_at: datetime) -> int:
        """Write the outcome and Brier scores onto every open ledger entry of a market."""
        ...

    def ledger(
        self,
        *,
        resolved_before: datetime | None = None,
        category: str | None = None,
        system: str | None = None,
    ) -> list[LedgerEntry]: ...

    def calibration(self, category: str, *, as_of: datetime) -> Calibration: ...

    # -- sources behind open positions
    def remember_document(self, doc_id: str, content_hash: str, seen_at: datetime) -> None: ...

    def document_hash(self, doc_id: str) -> str | None: ...

    # -- halt and stage
    def get_halt(self) -> HaltState: ...

    def set_halt(self, reason: str, at: datetime) -> None: ...

    def clear_halt(self) -> None: ...

    def get_stage(self) -> StageState: ...

    def set_stage(self, stage: Stage, reason: str, at: datetime) -> None: ...


@dataclass(frozen=True)
class World:
    """The set of ports an episode sees."""

    mode: RunMode
    config: Config
    clock: Clock
    exchange: Exchange
    executor: Executor
    evidence: Evidence
    knowledge: Knowledge
    models: Models
    store: StateStore
    review_policy: ReviewPolicy
