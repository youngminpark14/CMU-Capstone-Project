"""The book a backtest order meets: what the paper executor fills against in backtests.

The exchange's history has no depth data, only candles, so the book is built
from one candle after the order is sent, priced conservatively. It is for
filling only: no decision reads it. The pipeline decides with the as-of book
(``SnapshotExchange.get_order_book``, which the backtest world's
``Executor.execution_book`` returns), so nothing priced after the as-of time
reaches the re-read, the recompute, the gate or routing.

1. The order arrives at ``t = as_of + execution.backtest_latency_seconds``.
2. The candle used is the one-minute candle whose period contains ``t`` (the
   first one ending after ``t``, within a minute of it). If the snapshot has
   none there, the hourly candle containing ``t`` is used and the result is
   marked coarse, so reports can say how each fill was priced.
3. **At the ask, on the conservative side of the period.** Buying YES meets the
   candle's *highest* YES ask; buying NO meets 1 minus its *lowest* YES bid.
   An order is thus never filled better than the worst touch seen in that
   period. In book terms: a NO bid at ``1 - yes_ask.high`` and a YES bid at
   ``yes_bid.low``.
4. **Size.** Each level holds ``execution.backtest_volume_share`` of the
   candle's volume, floored to whole contracts.

The book is empty when the market has closed by ``t``, when no candle
contains ``t``, or when that candle has no volume. A side with no quote in the
candle (a YES ask of 1, a YES bid of 0, or none at all) is empty.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_FLOOR
from typing import Literal

from pmfa.config import Config
from pmfa.records.base import ONE, ZERO, Record, UtcTime
from pmfa.records.market import BookLevel, Candle, OrderBook
from pmfa.world.snapshot.store import SnapshotStore

Granularity = Literal["minute", "hour", "none"]


class ExecutionBook(Record):
    """The book an order would meet, the candle it came from, and how fine that candle was."""

    book: OrderBook
    candle: Candle | None = None
    granularity: Granularity = "none"
    """``minute``: priced from a one-minute candle; ``hour``: the coarse hourly fallback; ``none``: empty."""
    arrival: UtcTime
    """``as_of`` plus the configured latency: when the order reaches the exchange."""

    @property
    def coarse(self) -> bool:
        return self.granularity == "hour"


def execution_book(
    store: SnapshotStore, ticker: str, as_of: datetime, config: Config
) -> ExecutionBook:
    """The book an order sent at ``as_of`` meets. See the module docstring for the rule."""
    arrival = as_of + timedelta(seconds=config.execution.backtest_latency_seconds)
    market = store.get_market(ticker)
    empty = ExecutionBook(book=OrderBook(ticker=ticker, as_of=arrival), arrival=arrival)
    if arrival >= market.close_time:
        return empty
    granularity: Granularity = "minute"
    candle = store.first_candle(ticker, 1, after=arrival, until=arrival + timedelta(minutes=1))
    if candle is None:
        granularity = "hour"
        candle = store.first_candle(ticker, 60, after=arrival, until=arrival + timedelta(hours=1))
    if candle is None:
        return empty
    found = empty.model_copy(update={"candle": candle, "granularity": granularity})
    size = (candle.volume * config.execution.backtest_volume_share).to_integral_value(
        rounding=ROUND_FLOOR
    )
    if size <= 0:
        return found
    yes_bids, no_bids = [], []
    if candle.yes_bid is not None and candle.yes_bid.low > ZERO:
        yes_bids.append(BookLevel(price=candle.yes_bid.low, contracts=size))
    if candle.yes_ask is not None and candle.yes_ask.high < ONE:
        no_bids.append(BookLevel(price=ONE - candle.yes_ask.high, contracts=size))
    book = found.book.model_copy(update={"yes_bids": yes_bids, "no_bids": no_bids})
    return found.model_copy(update={"book": book})


__all__ = ["ExecutionBook", "Granularity", "execution_book"]
