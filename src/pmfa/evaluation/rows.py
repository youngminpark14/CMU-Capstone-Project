"""The plain rows the evaluation math works on.

The backtest runner and the ledger turn episodes into these rows; every metric,
interval, tally, and benchmark in ``pmfa.evaluation`` is a pure function of them.
Keeping the rows flat means the math can be checked on small hand-written cases.
"""

from __future__ import annotations

from typing import Self

from pydantic import model_validator

from pmfa.records.base import Count, Dollars, Price, Record, UtcTime
from pmfa.records.decision import RoutingTier
from pmfa.records.market import Side


class ForecastRow(Record):
    """One system's forecast for one market at one as-of time, with its resolution.

    A row is scheduled (``horizon_days`` set: 7, 3, or 1 days before close) or
    news-triggered (``trigger_id`` set). ``probability`` is ``None`` when the
    system abstained (an episode ended in a no-forecast exit); the metrics decide
    how such a row is scored.
    """

    run_id: str
    system: str
    """``market``, ``prompt_only``, ``single_pass``, ``pipeline``, or a ranking variant."""
    event_ticker: str
    market_ticker: str
    category: str
    horizon_days: int | None = None
    trigger_id: str | None = None
    as_of: UtcTime
    probability: float | None
    """Forecast probability of YES, or ``None`` when the system made no forecast."""
    market_price: float
    """The YES price at the as-of time: the market's own forecast."""
    outcome: bool
    """True when the market resolved YES."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.horizon_days is None and self.trigger_id is None:
            raise ValueError("a forecast row needs a horizon_days or a trigger_id")
        if self.probability is not None and not (0.0 <= self.probability <= 1.0):
            raise ValueError(f"probability {self.probability} is outside [0, 1]")
        if not (0.0 <= self.market_price <= 1.0):
            raise ValueError(f"market_price {self.market_price} is outside [0, 1]")
        return self

    @property
    def abstained(self) -> bool:
        return self.probability is None


class TradeRow(Record):
    """One fill and, once the market settles, its result.

    ``price`` is per contract on ``side``'s scale and ``fee`` is the fee paid for
    the whole fill, so the trade cost ``contracts x price + fee``. ``result`` is
    the side the market settled on; it and ``settled_at`` are set together.
    """

    episode_id: str
    event_ticker: str
    market_ticker: str
    side: Side
    contracts: Count
    price: Price
    fee: Dollars
    tier: RoutingTier
    would_be_auto: bool = False
    filled_at: UtcTime
    result: Side | None = None
    """The side the market settled on, or ``None`` while it is open."""
    settled_at: UtcTime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.contracts <= 0:
            raise ValueError("a trade row needs a positive number of contracts")
        if self.fee < 0:
            raise ValueError("a fee cannot be negative")
        if (self.result is None) != (self.settled_at is None):
            raise ValueError("result and settled_at are set together")
        if self.settled_at is not None and self.settled_at < self.filled_at:
            raise ValueError("a trade cannot settle before it fills")
        return self

    @property
    def settled(self) -> bool:
        return self.result is not None
