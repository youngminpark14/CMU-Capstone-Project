"""Records local to the decision core: what sizing, the gate, and routing take and return.

They are records (frozen, unknown fields rejected) so callers build them once
and the pure functions cannot change them.
"""

from __future__ import annotations

from decimal import Decimal

from pmfa.config import Config, SeriesConfig
from pmfa.records.base import Dollars, Price, Record, Stage, UtcTime
from pmfa.records.decision import Proposal
from pmfa.records.episode import Calibration, RiskState
from pmfa.records.evidence import Origin
from pmfa.records.market import Market, OrderBook, Position, Series, Side


class SizingInputs(Record):
    """Everything sizing needs for one event, gathered by the caller beforehand."""

    episode_id: str
    event_ticker: str
    forecast_yes: dict[str, float]
    """Forecast probability of YES per market ticker (before shrinkage)."""
    markets: list[Market]
    """The event's markets: their valid ticks come from ``price_ranges``."""
    books: dict[str, OrderBook]
    """The current order book per market ticker. A market without a book is not tradable."""
    liquidity: dict[str, dict[Side, Dollars]] = {}
    """Dollars resting near the touch per (market ticker, side). Missing means zero."""
    positions: list[Position] = []
    """Current holdings. Holdings in other events are ignored."""
    bankroll: Dollars
    confidence: float
    series: Series
    series_config: SeriesConfig
    created_at: UtcTime
    """The as-of time of the sizing, read from the clock by the caller."""
    protected_drove_size: bool = False
    """Copied onto the proposal; routing will not auto-route a protected-slot trade."""


class Candidate(Record):
    """One market-and-side that sizing considered, with its edge at the best ask."""

    market_ticker: str
    side: Side
    ask: Price
    shrunk_probability: Decimal
    edge: Decimal
    """Shrunk probability minus the ask minus the unrounded fee per contract."""
    liquidity: Dollars
    meets_liquidity: bool


class SizingDecision(Record):
    """The result of sizing: a proposal, or no trade with a reason."""

    proposal: Proposal | None = None
    reason: str = ""
    """Why there is no proposal. Empty when there is one."""
    best: Candidate | None = None
    """The chosen market-and-side, when one met the liquidity floor."""
    candidates: list[Candidate] = []

    @property
    def trades(self) -> bool:
        return self.proposal is not None


class GateContext(Record):
    """What the gate re-verifies a proposal against, read fresh by the caller."""

    config: Config
    now: UtcTime
    halted: bool
    stage: Stage
    simulated: bool
    """True when the order goes to the paper executor."""
    series: Series
    series_config: SeriesConfig
    market: Market
    book: OrderBook
    """The current book; the gate takes the YES mid from it."""
    forecast_yes: float
    """The forecast probability of YES for the proposal's market (before shrinkage)."""
    origins: list[Origin]
    liquidity_dollars: Dollars
    """Dollars resting near the touch on the proposal's side."""
    positions: list[Position] = []
    risk: RiskState


class RoutingContext(Record):
    """What routing needs besides the proposal."""

    config: Config
    stage: Stage
    halted: bool
    risk: RiskState
    liquidity_dollars: Dollars
    calibration: Calibration
    price_move: Decimal
    """How far the YES price moved during the episode, in dollars (sign ignored)."""
