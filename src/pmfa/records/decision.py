"""Decision records: proposals, the gate's verdict, routing, review, orders, and fills."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pmfa.records.base import Count, Dollars, Price, Record, UtcTime
from pmfa.records.market import Side


class SizingSummary(Record):
    """What sizing would do for a given forecast. The search uses it for the protected slot."""

    market_ticker: str | None = None
    side: Side | None = None
    contracts: int = 0
    cost: Dollars = Decimal("0")

    @property
    def trades(self) -> bool:
        return self.contracts > 0


class Proposal(Record):
    """A sized order intent: buy ``contracts`` of ``side`` at no more than ``limit_price``.

    Selling is expressed as buying the other side; the exchange nets the two.
    """

    proposal_id: str
    episode_id: str
    event_ticker: str
    market_ticker: str
    side: Side
    contracts: int
    limit_price: Price
    """Per contract, on ``side``'s price scale."""
    edge_bound_price: Price
    """The highest price on ``side``'s scale that still leaves the minimum edge after fees."""
    reference_price: Price
    """The best ask on ``side`` when the proposal was sized."""
    market_mid: Price
    """The YES price the forecast was shrunk toward."""
    forecast_probability: float
    """Probability that ``side`` wins, from the forecast."""
    shrunk_probability: float
    edge: Decimal
    """Shrunk probability minus the price paid minus the fee per contract."""
    fee: Dollars
    """Estimated taker fee for the whole order, rounded up to the cent."""
    cost: Dollars
    """Worst-case cash out: contracts times limit price, plus the fee."""
    confidence: float
    position_before: int
    """Signed (YES-positive) contracts held in this market before the order."""
    position_target: int
    bankroll: Dollars
    created_at: UtcTime
    expires_at: UtcTime
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["immediate_or_cancel"] = "immediate_or_cancel"
    protected_drove_size: bool = False


class GateCheck(Record):
    name: str
    passed: bool
    detail: str = ""


class GateResult(Record):
    """The gate approves or rejects. It never carries, and so never modifies, an order."""

    approved: bool
    reasons: list[str] = []
    """Names of the failed checks, in the order they are evaluated."""
    checks: list[GateCheck] = []


class RoutingTier(StrEnum):
    AUTO = "auto"
    APPROVE = "approve"
    HALT = "halt"


class Routing(Record):
    tier: RoutingTier
    would_be_auto: bool = False
    """True when every auto condition held but the stage is below autonomous."""
    score: int = 0
    """The risk score: how many auto conditions failed."""
    reasons: list[str] = []


class ReviewPolicy(StrEnum):
    """What happens to a proposal that needs a human."""

    PAUSE = "pause"  # persist it and wait for the review command
    FILL_TAGGED = "fill_tagged"  # no human present (backtests): fill it and tag the tier


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    RESIZE = "resize"
    REJECT = "reject"


class ReasonCode(StrEnum):
    STALE_EVIDENCE = "stale_evidence"
    WRONG_MARKET = "wrong_market"
    SIZE_TOO_LARGE = "size_too_large"
    DISAGREE_WITH_FORECAST = "disagree_with_forecast"
    OTHER = "other"


class ReviewCardPassage(Record):
    passage_id: str
    source: str
    title: str
    published_at: UtcTime
    excerpt: str


class ReviewCard(Record):
    """What the reviewer sees before choosing approve, resize, or reject."""

    proposal_id: str
    episode_id: str
    event_title: str
    market_title: str
    market_ticker: str
    side: Side
    contracts: int
    limit_price: Price
    cost: Dollars
    forecast_probability: float
    market_price: Price
    edge: Decimal
    confidence: float
    routing_reasons: list[str]
    passages: list[ReviewCardPassage]
    expires_at: UtcTime


class Review(Record):
    proposal_id: str
    decision: ReviewDecision
    contracts: int | None = None
    """The new size, for a resize. It may only shrink the proposal."""
    reason_code: ReasonCode | None = None
    note: str = ""
    reviewer: str = "human"
    decided_at: UtcTime


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    RESIZED = "resized"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"
    """Its order may stand on the exchange, unconfirmed: never resent, reviewed or expired."""


class OrderIntent(Record):
    """An immediate-or-cancel limit order to buy ``contracts`` of ``side``."""

    client_order_id: str
    """Deterministic, so a retry cannot double an order."""
    episode_id: str
    proposal_id: str
    market_ticker: str
    side: Side
    contracts: int
    limit_price: Price


class Fill(Record):
    fill_id: str
    order_id: str
    market_ticker: str
    side: Side
    contracts: Count
    price: Price
    fee: Dollars
    filled_at: UtcTime
    simulated: bool


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    UNFILLED = "unfilled"
    REJECTED = "rejected"


class OrderResult(Record):
    order_id: str
    client_order_id: str
    episode_id: str
    market_ticker: str
    side: Side
    requested_contracts: int
    limit_price: Price
    status: OrderStatus
    fills: list[Fill] = []
    error: str = ""
    placed_at: UtcTime
    outcome_at: UtcTime | None = None
    """When the order's outcome (its fills, or that it did not fill) was known.

    Set only when that is later than ``placed_at``: a backtest order is priced
    from a candle after it was sent, and nothing may count its outcome before then.
    """

    @property
    def filled_contracts(self) -> Count:
        return sum((f.contracts for f in self.fills), Decimal("0"))

    @property
    def fees(self) -> Dollars:
        return sum((f.fee for f in self.fills), Decimal("0"))
