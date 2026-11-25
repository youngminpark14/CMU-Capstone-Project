"""Wire JSON to records: the only place that knows Kalshi's field names.

Facts checked against the live API and the OpenAPI document on 2026-11-25:

- **Prices** are fixed-point dollar strings (``"0.0500"``; responses may carry up
  to six decimals, which the ``Price`` type rounds to four). **Counts** are
  fixed-point strings with two decimals (``"14219.15"``) and can be fractional.
- **Times** are RFC 3339 strings (``"2026-12-09T18:59:00Z"``, sometimes with
  microseconds); candle period ends are Unix seconds.
- **Market status** words on the object differ from the list-filter words:
  ``initialized`` (unopened), ``active`` (open), ``inactive`` (paused),
  ``closed`` / ``determined`` / ``disputed`` / ``amended`` (trading over, result
  not final), ``finalized`` (settled). ``result`` is ``""`` until determined,
  then ``yes`` or ``no`` (``scalar`` exists for scalar markets, mapped to None).
- **Quotes** use sentinels for an empty side: a YES bid of ``0.0000`` and a YES
  ask of ``1.0000`` mean nobody is quoting. They are kept as sent;
  :attr:`Market.mid` already ignores them.
- **Order book** (``GET /markets/{ticker}/orderbook``): ``{"orderbook_fp":
  {"yes_dollars": [[price, count], ...], "no_dollars": [...]}}``, bids only,
  sorted **ascending** (best bid last). An unknown ticker returns empty arrays,
  not 404. The record lists levels best first, so they are reversed here.
- **Candles**: the live endpoint names OHLC fields ``open_dollars`` ... and
  counts ``volume_fp`` / ``open_interest_fp``; the historical endpoint names
  them ``open`` ... and ``volume`` / ``open_interest``. A period without trades
  has no ``price`` OHLC (live: keys absent; historical: nulls), only
  ``previous``. Both shapes are read by the same function.
- **Positions** (``GET /portfolio/positions``): ``position_fp`` is signed,
  positive for YES and negative for NO; ``market_exposure_dollars`` is the cost
  basis of what remains, excluding fees. Market positions carry no event
  ticker; the event positions in the same response supply it.
- **Fills** (``GET /portfolio/fills``): direction is ``outcome_side`` (``yes`` /
  ``no``); ``yes_price_dollars`` and ``no_price_dollars`` give the price on each
  scale; ``fee_cost`` is the fee in dollars.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pmfa.errors import ExchangeError
from pmfa.records.base import PRICE_QUANTUM, ZERO
from pmfa.records.decision import Fill
from pmfa.records.market import (
    OHLC,
    Balance,
    BookLevel,
    Candle,
    Event,
    Market,
    MarketStatus,
    OrderBook,
    Position,
    PriceRange,
    Series,
    SettlementSource,
    Side,
)

Json = Mapping[str, Any]

STATUS_WORDS: dict[str, MarketStatus] = {
    # the market object's status field
    "initialized": MarketStatus.UNOPENED,
    "active": MarketStatus.OPEN,
    "inactive": MarketStatus.PAUSED,
    "closed": MarketStatus.CLOSED,
    "determined": MarketStatus.CLOSED,
    "disputed": MarketStatus.CLOSED,
    "amended": MarketStatus.CLOSED,
    "finalized": MarketStatus.SETTLED,
    # the list-filter words, accepted too
    "unopened": MarketStatus.UNOPENED,
    "open": MarketStatus.OPEN,
    "paused": MarketStatus.PAUSED,
    "settled": MarketStatus.SETTLED,
}

CANDLE_INTERVALS = (1, 60, 1440)


@dataclass(frozen=True)
class HistoricalCutoff:
    """``GET /historical/cutoff``: data older than these moments lives on the historical endpoints."""

    market_settled_ts: datetime
    """Markets (and their candles) that settled before this are served by ``/historical/markets``."""
    trades_created_ts: datetime | None = None
    orders_updated_ts: datetime | None = None
    market_positions_last_updated_ts: datetime | None = None


# -- scalars


def decimal(value: Any) -> Decimal:
    """A fixed-point string (or JSON number) as a Decimal. Floats go through ``str``."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ExchangeError(f"expected a number, got {value!r}")
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ExchangeError(f"not a fixed-point number: {value!r}") from exc


def optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return decimal(value)


def price(value: Any) -> Decimal | None:
    """A dollar price string as a Decimal, or None when absent. Range-checked by the records."""
    return optional_decimal(value)


def count(value: Any) -> Decimal:
    """A fixed-point contract count; absent means zero."""
    found = optional_decimal(value)
    return ZERO if found is None else found


def utc(value: Any) -> datetime | None:
    """An RFC 3339 string or Unix seconds as an aware UTC datetime; empty means None."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ExchangeError(f"not an RFC 3339 time: {value!r}") from exc
    if moment.tzinfo is None:
        raise ExchangeError(f"time without an offset: {value!r}")
    return moment.astimezone(UTC)


def required_utc(value: Any, field: str) -> datetime:
    moment = utc(value)
    if moment is None:
        raise ExchangeError(f"missing time field {field}")
    return moment


def unix_seconds(moment: datetime) -> int:
    """Whole Unix seconds, rounded down."""
    return int(moment.timestamp() // 1)


def market_status(word: str) -> MarketStatus:
    try:
        return STATUS_WORDS[word]
    except KeyError:
        raise ExchangeError(f"unknown market status {word!r}") from None


def result_side(word: Any) -> Side | None:
    """``"yes"`` / ``"no"`` to a side; ``""`` (not determined) and ``"scalar"`` to None."""
    if word == "yes":
        return Side.YES
    if word == "no":
        return Side.NO
    return None


# -- reference data


def parse_series(body: Json) -> Series:
    """``GET /series/{ticker}`` (the body or its ``series`` object)."""
    obj = body.get("series", body)
    return Series(
        ticker=obj["ticker"],
        title=obj.get("title") or "",
        category=obj.get("category") or "",
        frequency=obj.get("frequency") or "",
        fee_type=obj.get("fee_type") or "",
        fee_multiplier=decimal(obj.get("fee_multiplier", 1)),
        settlement_sources=[
            SettlementSource(name=s.get("name") or "", url=s.get("url") or "")
            for s in obj.get("settlement_sources") or []
        ],
    )


def parse_market(obj: Json) -> Market:
    """One market object, from the live or the historical endpoints."""
    return Market(
        ticker=obj["ticker"],
        event_ticker=obj["event_ticker"],
        title=obj.get("title") or "",
        subtitle=obj.get("yes_sub_title") or obj.get("subtitle") or "",
        status=market_status(obj["status"]),
        open_time=required_utc(obj.get("open_time"), "open_time"),
        close_time=required_utc(obj.get("close_time"), "close_time"),
        strike_type=obj.get("strike_type") or "",
        floor_strike=optional_decimal(obj.get("floor_strike")),
        cap_strike=optional_decimal(obj.get("cap_strike")),
        custom_strike={str(k): str(v) for k, v in (obj.get("custom_strike") or {}).items()},
        rules_primary=obj.get("rules_primary") or "",
        rules_secondary=obj.get("rules_secondary") or "",
        price_ranges=[
            PriceRange(start=decimal(r["start"]), end=decimal(r["end"]), step=decimal(r["step"]))
            for r in obj.get("price_ranges") or []
        ],
        yes_bid=price(obj.get("yes_bid_dollars")),
        yes_ask=price(obj.get("yes_ask_dollars")),
        last_price=price(obj.get("last_price_dollars")),
        yes_bid_size=optional_decimal(obj.get("yes_bid_size_fp")),
        yes_ask_size=optional_decimal(obj.get("yes_ask_size_fp")),
        volume=count(obj.get("volume_fp")),
        volume_24h=count(obj.get("volume_24h_fp")),
        open_interest=count(obj.get("open_interest_fp")),
        result=result_side(obj.get("result")),
        settled_at=utc(obj.get("settlement_ts")),
    )


def parse_event(obj: Json, markets: Iterable[Market] | None = None) -> Event:
    """One event object. ``markets`` replaces the nested ``markets`` list when given."""
    if markets is None:
        markets = [parse_market(m) for m in obj.get("markets") or []]
    ordered = sorted(markets, key=lambda m: (m.close_time, m.ticker))
    return Event(
        event_ticker=obj["event_ticker"],
        series_ticker=obj["series_ticker"],
        title=obj.get("title") or "",
        sub_title=obj.get("sub_title") or "",
        category=obj.get("category") or "",
        mutually_exclusive=bool(obj.get("mutually_exclusive", False)),
        markets=ordered,
    )


def event_markets(body: Json) -> list[Market]:
    """The markets of a ``GET /events/{ticker}`` response.

    With ``with_nested_markets=true`` they sit under ``event.markets`` and the
    top-level ``markets`` is empty; without it, the reverse.
    """
    nested = (body.get("event") or {}).get("markets") or []
    top = body.get("markets") or []
    return [parse_market(m) for m in (nested or top)]


def merge_markets(*groups: Iterable[Market]) -> list[Market]:
    """Union by ticker; the first group to name a ticker wins (pass live before historical)."""
    merged: dict[str, Market] = {}
    for group in groups:
        for market in group:
            merged.setdefault(market.ticker, market)
    return list(merged.values())


def parse_cutoff(body: Json) -> HistoricalCutoff:
    return HistoricalCutoff(
        market_settled_ts=required_utc(body.get("market_settled_ts"), "market_settled_ts"),
        trades_created_ts=utc(body.get("trades_created_ts")),
        orders_updated_ts=utc(body.get("orders_updated_ts")),
        market_positions_last_updated_ts=utc(body.get("market_positions_last_updated_ts")),
    )


# -- order book and candles


def _levels(raw: Iterable[Any]) -> list[BookLevel]:
    levels = [BookLevel(price=decimal(p), contracts=decimal(c)) for p, c in raw]
    levels = [lvl for lvl in levels if lvl.contracts > 0]
    return sorted(levels, key=lambda lvl: lvl.price, reverse=True)


def parse_orderbook(body: Json, ticker: str, as_of: datetime) -> OrderBook:
    """``GET /markets/{ticker}/orderbook``: bid levels per side, best (highest) first."""
    book = body.get("orderbook_fp")
    if not isinstance(book, Mapping):
        raise ExchangeError("order book response has no orderbook_fp object")
    return OrderBook(
        ticker=ticker,
        as_of=as_of,
        yes_bids=_levels(book.get("yes_dollars") or []),
        no_bids=_levels(book.get("no_dollars") or []),
    )


def _field(obj: Json, name: str) -> Any:
    """Live candles say ``open_dollars``, historical ones ``open``."""
    value = obj.get(f"{name}_dollars")
    return obj.get(name) if value is None else value


def _ohlc(obj: Json | None) -> OHLC | None:
    if not obj:
        return None
    values = [price(_field(obj, k)) for k in ("open", "high", "low", "close")]
    if any(v is None for v in values):
        return None
    o, h, low, c = values
    return OHLC(open=o, high=h, low=low, close=c)


def parse_candle(obj: Json, ticker: str, interval_minutes: int) -> Candle:
    volume = obj.get("volume_fp", obj.get("volume"))
    open_interest = obj.get("open_interest_fp", obj.get("open_interest"))
    return Candle(
        ticker=ticker,
        end_ts=required_utc(obj.get("end_period_ts"), "end_period_ts"),
        interval_minutes=interval_minutes,
        yes_bid=_ohlc(obj.get("yes_bid")),
        yes_ask=_ohlc(obj.get("yes_ask")),
        price=_ohlc(obj.get("price")),
        volume=count(volume),
        open_interest=optional_decimal(open_interest),
    )


def parse_candles(body: Json, ticker: str, interval_minutes: int) -> list[Candle]:
    """Either candlestick endpoint's response, oldest first."""
    candles = [parse_candle(c, ticker, interval_minutes) for c in body.get("candlesticks") or []]
    return sorted(candles, key=lambda c: c.end_ts)


# -- portfolio (private endpoints; shapes from the OpenAPI document)


def _event_for(ticker: str, event_tickers: Iterable[str]) -> str:
    """The event a market ticker belongs to: the longest event ticker that prefixes it."""
    matches = [e for e in event_tickers if ticker.startswith(f"{e}-")]
    if matches:
        return max(matches, key=len)
    return ticker.rsplit("-", 1)[0]


def parse_position(obj: Json, event_ticker: str) -> Position | None:
    """One ``market_positions`` entry; None when the net position is zero.

    ``position_fp`` is signed (YES positive). Fractional holdings are truncated
    toward zero, since v1 trades whole contracts.
    """
    signed = count(obj.get("position_fp"))
    contracts = int(abs(signed))
    if contracts == 0:
        return None
    exposure = count(obj.get("market_exposure_dollars"))
    avg = (exposure / abs(signed)).quantize(PRICE_QUANTUM)
    return Position(
        market_ticker=obj["ticker"],
        event_ticker=event_ticker,
        side=Side.YES if signed > 0 else Side.NO,
        contracts=contracts,
        avg_price=min(max(avg, ZERO), Decimal("1")),
    )


def parse_positions(pages: Iterable[Json]) -> list[Position]:
    """All pages of ``GET /portfolio/positions`` to positions, with event tickers filled in."""
    market_rows: list[Json] = []
    events: set[str] = set()
    for page in pages:
        market_rows.extend(page.get("market_positions") or [])
        events.update(e["event_ticker"] for e in page.get("event_positions") or [])
    positions: dict[str, Position] = {}
    for row in market_rows:
        position = parse_position(row, _event_for(row["ticker"], events))
        if position is not None:
            positions[position.market_ticker] = position
    return sorted(positions.values(), key=lambda p: p.market_ticker)


def parse_fill(obj: Json) -> Fill:
    """One ``GET /portfolio/fills`` entry, priced on its own side's scale."""
    side = Side(obj["outcome_side"])
    raw_price = obj.get("yes_price_dollars") if side is Side.YES else obj.get("no_price_dollars")
    filled_at = utc(obj.get("created_time")) or utc(obj.get("ts"))
    if filled_at is None:
        raise ExchangeError("fill without a time")
    return Fill(
        fill_id=obj.get("fill_id") or obj["trade_id"],
        order_id=obj["order_id"],
        market_ticker=obj.get("ticker") or obj["market_ticker"],
        side=side,
        contracts=count(obj.get("count_fp")),
        price=decimal(raw_price),
        fee=count(obj.get("fee_cost")),
        filled_at=filled_at,
        simulated=False,
    )


def parse_balance(body: Json, positions: Iterable[Position]) -> Balance:
    """``GET /portfolio/balance`` plus open positions at cost: equity is cash plus cost basis."""
    raw = body.get("balance_dollars")
    cash = decimal(raw) if raw is not None else decimal(body.get("balance", 0)) / 100
    at_cost = sum((p.cost_basis for p in positions), ZERO)
    return Balance(cash=cash, equity=cash + at_cost)


__all__ = [
    "CANDLE_INTERVALS",
    "STATUS_WORDS",
    "HistoricalCutoff",
    "count",
    "decimal",
    "event_markets",
    "market_status",
    "merge_markets",
    "parse_balance",
    "parse_candle",
    "parse_candles",
    "parse_cutoff",
    "parse_event",
    "parse_fill",
    "parse_market",
    "parse_orderbook",
    "parse_position",
    "parse_positions",
    "parse_series",
    "price",
    "result_side",
    "unix_seconds",
    "utc",
]
