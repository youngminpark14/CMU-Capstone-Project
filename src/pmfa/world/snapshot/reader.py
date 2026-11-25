"""The ``Exchange`` port over a snapshot, read as of a clock. It never shows the future.

The snapshot holds each market's *final* record. Everything that changes over
time is recomputed here from the clock's time ``now``:

- **Listing.** A market is listed once its open time has passed. Event views
  (``get_event``, ``list_events``) leave out markets not yet open, so a ladder
  strike added later does not appear early; an event with no market open yet
  is not found. ``get_market`` by ticker still answers, with ``unopened``.
- **Status.** Unopened before the open time, open until the close time, closed
  until the settlement time, settled after. ``result`` and ``settled_at`` are
  ``None`` until ``now`` reaches the settlement time.
- **Quotes.** From the latest candle that ended at or before ``now``: the more
  recent of the latest one-minute and the latest hourly candle, the one-minute
  candle winning a tie. (Candles exist only for periods in which something
  changed, so inside the one-minute range the latest one-minute candle *is*
  the current state; before it, the hourly candle is.) YES bid and ask are
  that candle's closes; the last price is the close of the latest candle with
  a trade.
- **Volume.** Cumulative volume is the sum of hourly volumes up to the last
  hourly candle at or before ``now`` plus one-minute volumes after it; the
  24-hour volume is the same over the last day. Volume before the first stored
  candle (more than 30 days before the window start) is not counted.
- **Order book.** One level per side, synthesised from that latest candle:
  a YES bid at its YES bid close and a NO bid at 1 minus its YES ask close,
  each sized at ``execution.backtest_volume_share`` times the contracts traded
  in the trailing ``execution.backtest_liquidity_window_minutes``, floored to
  whole contracts. A side with no quote (bid 0, ask 1) or no size is empty.
- **Liquidity.** The candle-volume proxy the spec allows: dollars traded on
  ``side``'s price scale (volume times the candle's trade close) in that
  trailing window.

Candles are served at the stored intervals only (1 and 60 minutes).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from pmfa.config import Config
from pmfa.errors import NotFound
from pmfa.ports import Clock
from pmfa.records.base import ONE, ZERO
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
from pmfa.world.snapshot.store import INTERVALS, SnapshotStore, from_ts

_DAY = timedelta(days=1)
_EPOCH = from_ts(0)


def market_status(market: Market, now: datetime) -> MarketStatus:
    """The status a market had at ``now``, from its open, close and settlement times."""
    if now < market.open_time:
        return MarketStatus.UNOPENED
    if now < market.close_time:
        return MarketStatus.OPEN
    if market.settled_at is None or now < market.settled_at:
        return MarketStatus.CLOSED
    return MarketStatus.SETTLED


class SnapshotExchange:
    """Read-only market data from a snapshot file, as of ``clock.now()``."""

    def __init__(self, store: SnapshotStore, clock: Clock, config: Config) -> None:
        self.store = store
        self.clock = clock
        self.config = config

    # -- Exchange port

    def get_series(self, series_ticker: str) -> Series:
        return self.store.get_series(series_ticker)

    def list_events(self, series_ticker: str, *, include_closed: bool = False) -> list[Event]:
        """Events with a market open now; ``include_closed`` adds those already closed by now."""
        now = self.clock.now()
        events = []
        for event_ticker in self.store.event_tickers(series_ticker):
            event = self._event_as_of(event_ticker, now)
            if event is None:
                continue
            is_open = any(m.status in (MarketStatus.OPEN, MarketStatus.PAUSED) for m in event.markets)
            if is_open or include_closed:
                events.append(event)
        return sorted(events, key=lambda e: (e.close_time, e.event_ticker))

    def get_event(self, event_ticker: str) -> Event:
        event = self._event_as_of(event_ticker, self.clock.now())
        if event is None:
            raise NotFound(f"event {event_ticker} has no market open yet")
        return event

    def get_market(self, ticker: str) -> Market:
        return self._market_as_of(self.store.get_market(ticker), self.clock.now())

    def get_order_book(self, ticker: str) -> OrderBook:
        now = self.clock.now()
        market = self.store.get_market(ticker)
        candle = self._latest_quote(ticker, now)
        book = OrderBook(ticker=ticker, as_of=now)
        if candle is None or market_status(market, now) is not MarketStatus.OPEN:
            return book
        size = self._trailing_size(ticker, now)
        if size <= 0:
            return book
        yes_bids, no_bids = [], []
        if candle.yes_bid is not None and candle.yes_bid.close > ZERO:
            yes_bids.append(BookLevel(price=candle.yes_bid.close, contracts=size))
        if candle.yes_ask is not None and candle.yes_ask.close < ONE:
            no_bids.append(BookLevel(price=ONE - candle.yes_ask.close, contracts=size))
        return book.model_copy(update={"yes_bids": yes_bids, "no_bids": no_bids})

    def get_candles(
        self, ticker: str, *, start: datetime, end: datetime, interval_minutes: int
    ) -> list[Candle]:
        """Candles ending in ``(start, end]``, ``end`` clamped to the clock's time."""
        if interval_minutes not in INTERVALS:
            raise ValueError(f"the snapshot holds candle intervals {INTERVALS}")
        self.store.get_market(ticker)  # NotFound for an unknown ticker
        end = min(end, self.clock.now())
        if end <= start:
            return []
        return self.store.candles(ticker, interval_minutes, after=start, until=end)

    def liquidity_dollars(self, ticker: str, side: Side) -> Decimal:
        """Dollars traded on ``side``'s scale in the trailing liquidity window (a candle-volume proxy)."""
        now = self.clock.now()
        self.store.get_market(ticker)
        total = ZERO
        for candle in self._trailing_candles(ticker, now):
            if candle.price is None or candle.volume <= 0:
                continue
            px = candle.price.close if side is Side.YES else ONE - candle.price.close
            total += candle.volume * px
        return total

    # -- internals

    def _event_as_of(self, event_ticker: str, now: datetime) -> Event | None:
        final = self.store.get_event(event_ticker)
        listed = [self._market_as_of(m, now) for m in final.markets if m.open_time <= now]
        if not listed:
            return None
        return final.model_copy(update={"markets": listed})

    def _market_as_of(self, market: Market, now: datetime) -> Market:
        status = market_status(market, now)
        settled = status is MarketStatus.SETTLED
        quote = self._latest_quote(market.ticker, now)
        traded = self._latest_trade(market.ticker, now)
        oi = None if quote is None else quote.open_interest
        return market.model_copy(
            update={
                "status": status,
                "result": market.result if settled else None,
                "settled_at": market.settled_at if settled else None,
                "yes_bid": None if quote is None or quote.yes_bid is None else quote.yes_bid.close,
                "yes_ask": None if quote is None or quote.yes_ask is None else quote.yes_ask.close,
                "last_price": None if traded is None or traded.price is None else traded.price.close,
                "yes_bid_size": None,
                "yes_ask_size": None,
                "volume": self._volume_between(market.ticker, None, now),
                "volume_24h": self._volume_between(market.ticker, now - _DAY, now),
                "open_interest": ZERO if oi is None else oi,
            }
        )

    def _latest_quote(self, ticker: str, now: datetime) -> Candle | None:
        minute = self.store.latest_candle(ticker, 1, until=now)
        hour = self.store.latest_candle(ticker, 60, until=now)
        if minute is None or (hour is not None and hour.end_ts > minute.end_ts):
            return hour
        return minute

    def _latest_trade(self, ticker: str, now: datetime) -> Candle | None:
        minute = self.store.latest_candle(ticker, 1, until=now, traded=True)
        hour = self.store.latest_candle(ticker, 60, until=now, traded=True)
        if minute is None or (hour is not None and hour.end_ts > minute.end_ts):
            return hour
        return minute

    def _volume_between(self, ticker: str, after: datetime | None, until: datetime) -> Decimal:
        """Contracts traded in ``(after, until]``: hourly candles, then one-minute ones after them."""
        floor = after if after is not None else _EPOCH
        last_hour = self.store.latest_candle(ticker, 60, until=until)
        if last_hour is None or last_hour.end_ts <= floor:
            return self.store.volume(ticker, 1, after=floor, until=until)
        hourly = self.store.volume(ticker, 60, after=floor, until=last_hour.end_ts)
        return hourly + self.store.volume(ticker, 1, after=last_hour.end_ts, until=until)

    def _trailing_candles(self, ticker: str, now: datetime) -> list[Candle]:
        """Candles in the trailing liquidity window: one-minute where covered, else hourly."""
        since = now - timedelta(minutes=self.config.execution.backtest_liquidity_window_minutes)
        span = self.store.coverage(ticker, 1)
        if span is not None and span[0] <= since:
            return self.store.candles(ticker, 1, after=since, until=now)
        return self.store.candles(ticker, 60, after=since, until=now)

    def _trailing_size(self, ticker: str, now: datetime) -> Decimal:
        traded = sum((c.volume for c in self._trailing_candles(ticker, now)), ZERO)
        share = self.config.execution.backtest_volume_share
        return (traded * share).to_integral_value(rounding=ROUND_FLOOR)


__all__ = ["SnapshotExchange", "market_status"]
