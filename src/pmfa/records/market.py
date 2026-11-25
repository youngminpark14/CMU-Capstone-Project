"""Exchange records: series, events, markets, order books, candles, holdings.

These are the project's own shapes. The Kalshi adapters translate the wire
format into them, so nothing outside ``pmfa.world.kalshi`` sees a raw response.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pmfa.records.base import ONE, Count, Dollars, Price, Record, UtcTime


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def opposite(self) -> Side:
        return Side.NO if self is Side.YES else Side.YES


class MarketStatus(StrEnum):
    """Normalized life cycle of a market (the exchange uses several words for each)."""

    UNOPENED = "unopened"
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"  # trading has ended; the result may not be final yet
    SETTLED = "settled"


class SettlementSource(Record):
    name: str
    url: str


class Series(Record):
    ticker: str
    title: str
    category: str
    frequency: str = ""
    fee_type: str
    """As the exchange reports it, e.g. ``quadratic_with_maker_fees``."""
    fee_multiplier: Decimal
    settlement_sources: list[SettlementSource] = []


class PriceRange(Record):
    """Valid limit prices between ``start`` and ``end`` are multiples of ``step``."""

    start: Price
    end: Price
    step: Decimal


class Market(Record):
    ticker: str
    event_ticker: str
    title: str
    subtitle: str = ""
    """The short label of the YES outcome, e.g. "Fed maintains rate" or "Above 3.6%"."""
    status: MarketStatus
    open_time: UtcTime
    close_time: UtcTime
    strike_type: str = ""
    """As the exchange reports it: ``custom``, ``greater``, ``less``, ``between``, ..."""
    floor_strike: Decimal | None = None
    cap_strike: Decimal | None = None
    custom_strike: dict[str, str] = {}
    rules_primary: str = ""
    rules_secondary: str = ""
    price_ranges: list[PriceRange] = []
    yes_bid: Price | None = None
    yes_ask: Price | None = None
    last_price: Price | None = None
    yes_bid_size: Count | None = None
    yes_ask_size: Count | None = None
    volume: Count = Decimal("0")
    volume_24h: Count = Decimal("0")
    open_interest: Count = Decimal("0")
    result: Side | None = None
    """The settled result. ``None`` until the market settles (as of the world's clock)."""
    settled_at: UtcTime | None = None

    @property
    def mid(self) -> Price | None:
        """Midpoint of the YES bid and ask when both sides are quoted, else the last price."""
        bid, ask = self.yes_bid, self.yes_ask
        if bid is not None and ask is not None and bid > 0 and ask < ONE:
            return (bid + ask) / 2
        return self.last_price


class Event(Record):
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str = ""
    category: str = ""
    mutually_exclusive: bool = False
    markets: list[Market] = []

    @property
    def open_time(self) -> UtcTime:
        return min(m.open_time for m in self.markets)

    @property
    def close_time(self) -> UtcTime:
        return max(m.close_time for m in self.markets)

    def market(self, ticker: str) -> Market:
        for m in self.markets:
            if m.ticker == ticker:
                return m
        raise KeyError(ticker)


class BookLevel(Record):
    price: Price
    contracts: Count


class OrderBook(Record):
    """Resting bids on each side of one market, best (highest) price first.

    The exchange lists bids only. Buying one side means matching the other
    side's bids: a NO bid at ``q`` is an offer to sell YES at ``1 - q``.
    """

    ticker: str
    as_of: UtcTime
    yes_bids: list[BookLevel] = []
    no_bids: list[BookLevel] = []

    def bids(self, side: Side) -> list[BookLevel]:
        return self.yes_bids if side is Side.YES else self.no_bids

    def asks(self, side: Side) -> list[BookLevel]:
        """Offers to sell ``side``, cheapest first, on ``side``'s own price scale."""
        other = self.bids(side.opposite)
        return [BookLevel(price=ONE - lvl.price, contracts=lvl.contracts) for lvl in other]

    def best_bid(self, side: Side) -> BookLevel | None:
        levels = self.bids(side)
        return levels[0] if levels else None

    def best_ask(self, side: Side) -> BookLevel | None:
        levels = self.asks(side)
        return levels[0] if levels else None

    def mid_yes(self) -> Price | None:
        bid, ask = self.best_bid(Side.YES), self.best_ask(Side.YES)
        if bid is None or ask is None:
            return None
        return (bid.price + ask.price) / 2

    def resting_dollars_near_touch(self, side: Side, band: Decimal) -> Dollars:
        """Dollars offered on ``side`` within ``band`` of its best ask."""
        asks = self.asks(side)
        if not asks:
            return Decimal("0")
        limit = asks[0].price + band
        return sum((lvl.price * lvl.contracts for lvl in asks if lvl.price <= limit), Decimal("0"))


class OHLC(Record):
    open: Price
    high: Price
    low: Price
    close: Price


class Candle(Record):
    """One candle. ``end_ts`` is the end of the period; empty sides are ``None``."""

    ticker: str
    end_ts: UtcTime
    interval_minutes: int
    yes_bid: OHLC | None = None
    yes_ask: OHLC | None = None
    price: OHLC | None = None
    volume: Count = Decimal("0")
    open_interest: Count | None = None


class Position(Record):
    """A net holding in one market. Contracts are whole and positive; ``side`` says which."""

    market_ticker: str
    event_ticker: str
    side: Side
    contracts: int
    avg_price: Price
    """Average price paid per contract, on ``side``'s scale."""

    @property
    def signed(self) -> int:
        """YES-positive signed contracts: +n for YES, -n for NO."""
        return self.contracts if self.side is Side.YES else -self.contracts

    @property
    def cost_basis(self) -> Dollars:
        return self.avg_price * self.contracts


class Balance(Record):
    cash: Dollars
    equity: Dollars
    """Cash plus open positions at cost. This is the bankroll that sizing uses."""
