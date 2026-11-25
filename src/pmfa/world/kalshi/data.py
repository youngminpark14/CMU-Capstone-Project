"""Read-only Kalshi market data behind the ``Exchange`` port.

:class:`KalshiData` serves series, events, markets, order books, candles and
liquidity from public endpoints, on whichever host its client points at
(production for shadow mode, demo for demo mode). It has no order, cancel or
portfolio methods, and it never needs a signer.

Kalshi splits its data at a moving **historical cutoff** (``GET
/historical/cutoff``): markets that settled before ``market_settled_ts``, and
their candles, disappear from the live endpoints (which answer 404) and are
served by ``/historical/markets...``. Events and series stay on the live
endpoints, but an archived event lists no markets there. Every read below
therefore merges or falls back across the two sides, so callers never need
to know where a market currently lives.

Reads are "as of the world's clock": the order book is stamped with
``clock.now()``, and candle and event windows are clamped to it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pmfa.config import Config
from pmfa.errors import ExchangeError, NotFound
from pmfa.ports import Clock
from pmfa.records.market import Candle, Event, Market, MarketStatus, OrderBook, Series, Side
from pmfa.world.kalshi.client import KalshiClient
from pmfa.world.kalshi.parse import (
    CANDLE_INTERVALS,
    HistoricalCutoff,
    event_markets,
    merge_markets,
    parse_candles,
    parse_cutoff,
    parse_event,
    parse_market,
    parse_orderbook,
    parse_series,
    unix_seconds,
)

EVENTS_PAGE_LIMIT = 200
"""``GET /events`` allows at most 200 per page (checked: 201 is a 400)."""
MARKETS_PAGE_LIMIT = 1000
"""``GET /markets`` and ``GET /historical/markets`` allow at most 1000 per page."""
MAX_CANDLES_PER_REQUEST = 5000
"""Both candlestick endpoints reject a range of more than 5000 periods with a 400."""
CUTOFF_TTL = timedelta(minutes=5)
"""How long a fetched cutoff is reused. It advances about once a day."""

_LIVE_STATUSES = frozenset({MarketStatus.UNOPENED, MarketStatus.OPEN, MarketStatus.PAUSED})


class KalshiData:
    """The ``Exchange`` port over Kalshi's public REST endpoints."""

    def __init__(self, client: KalshiClient, clock: Clock, config: Config) -> None:
        self.client = client
        self.clock = clock
        self.config = config
        self._cutoff: tuple[datetime, HistoricalCutoff] | None = None
        self._event_series: dict[str, str] = {}
        self._settled_at: dict[str, datetime | None] = {}
        self._market_event: dict[str, str] = {}

    # -- Exchange port

    def get_series(self, series_ticker: str) -> Series:
        return parse_series(self.client.get(f"/series/{series_ticker}"))

    def list_events(self, series_ticker: str, *, include_closed: bool = False) -> list[Event]:
        """Events of a series with their markets, open events only unless asked.

        Open: ``GET /events?status=open&with_nested_markets=true`` (an event is
        open when any of its markets is). With ``include_closed``, every event
        of the series; markets behind the cutoff are added from one paged read
        of ``GET /historical/markets?series_ticker=...``. Events left with no
        market on either side are dropped, since an event is defined by its
        markets.
        """
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "with_nested_markets": True,
            "limit": EVENTS_PAGE_LIMIT,
        }
        if not include_closed:
            params["status"] = "open"
        raw_events = list(self.client.paginate("/events", params, "events"))
        historical: dict[str, list[Market]] = {}
        if include_closed:
            historical = self._historical_markets_by_event(series_ticker)
        events = []
        for raw in raw_events:
            live = [parse_market(m) for m in raw.get("markets") or []]
            markets = merge_markets(live, historical.get(raw["event_ticker"], []))
            if markets:
                events.append(parse_event(raw, markets))
        return sorted(events, key=lambda e: (e.close_time, e.event_ticker))

    def get_event(self, event_ticker: str) -> Event:
        """One event with its markets, rules, strikes and quotes.

        Reads ``GET /events/{ticker}?with_nested_markets=true``. When none of
        the live markets is still trading (or none is listed, as for an archived
        event), ``GET /historical/markets?event_ticker=...`` is merged in, live
        objects winning on a shared ticker. Raises ``NotFound`` for an unknown
        event or one with no market on either side.
        """
        body = self.client.get(f"/events/{event_ticker}", {"with_nested_markets": True})
        raw = body.get("event") or {}
        if not raw:
            raise ExchangeError(f"event response for {event_ticker} has no event object")
        live = event_markets(body)
        markets = live
        if not any(m.status in _LIVE_STATUSES for m in live):
            archived = [
                parse_market(m)
                for m in self.client.paginate(
                    "/historical/markets",
                    {"event_ticker": event_ticker, "limit": MARKETS_PAGE_LIMIT},
                    "markets",
                )
            ]
            markets = merge_markets(live, archived)
        if not markets:
            raise NotFound(f"event {event_ticker} has no markets on the live or historical side")
        event = parse_event(raw, markets)
        self._event_series[event.event_ticker] = event.series_ticker
        return event

    def get_market(self, ticker: str) -> Market:
        """``GET /markets/{ticker}``, falling back to ``GET /historical/markets/{ticker}`` on a 404."""
        try:
            market = parse_market(self.client.get(f"/markets/{ticker}")["market"])
        except NotFound:
            market = parse_market(self.client.get(f"/historical/markets/{ticker}")["market"])
        self._settled_at[ticker] = market.settled_at
        self._market_event[ticker] = market.event_ticker
        return market

    def get_order_book(self, ticker: str) -> OrderBook:
        """The book now, stamped with the clock's time.

        The exchange answers an unknown ticker with an empty book rather than a
        404, so an empty book does not prove the market exists.
        """
        body = self.client.get(f"/markets/{ticker}/orderbook")
        return parse_orderbook(body, ticker, self.clock.now())

    def get_candles(
        self,
        ticker: str,
        *,
        start: datetime,
        end: datetime,
        interval_minutes: int,
        series_ticker: str | None = None,
    ) -> list[Candle]:
        """Candles whose period ends in ``(start, end]``, from whichever side holds the market.

        The algorithm:

        1. Clamp ``end`` to the clock's time; an empty window returns ``[]``.
        2. Route. Look the market up (``get_market``: live, then historical on a
           404) and read the cutoff (``GET /historical/cutoff``, reused for five
           minutes). The market is *historical* when it settled before
           ``market_settled_ts``, else *live*. The settlement time is remembered
           per ticker, so later calls skip the lookup.
        3. Chunk. Split ``(start, end]`` into consecutive windows of at most
           5000 periods (the per-request maximum; 1-minute candles therefore
           come about 3.5 days at a time). Each window ``(a, b]`` is requested
           as ``start_ts = a + 1``, ``end_ts = b`` in whole Unix seconds, since
           the exchange includes candles ending on or after ``start_ts`` and on
           or before ``end_ts``.
        4. Fetch each window from the routed side: live is
           ``GET /series/{series}/markets/{ticker}/candlesticks`` (the series
           comes from ``series_ticker`` or the market's event), historical is
           ``GET /historical/markets/{ticker}/candlesticks``. If a window gets a
           404 because the cutoff moved since routing, switch sides once and
           repeat that window; the remaining windows use the new side.
        5. Keep candles ending in ``(start, end]``, one per end time, oldest first.

        The exchange omits periods in which nothing changed, so the result can
        have gaps; a period with no trades has ``price = None``.
        """
        if interval_minutes not in CANDLE_INTERVALS:
            raise ValueError(f"interval_minutes must be one of {CANDLE_INTERVALS}")
        end = min(end, self.clock.now())
        if end <= start:
            return []
        historical = self._is_historical(ticker)
        series = series_ticker
        span = MAX_CANDLES_PER_REQUEST * interval_minutes * 60
        lo, hi = unix_seconds(start), unix_seconds(end)
        candles: dict[datetime, Candle] = {}
        switched = False
        a = lo
        while a < hi:
            b = min(a + span, hi)
            params = {"start_ts": a + 1, "end_ts": b, "period_interval": interval_minutes}
            if not historical and series is None:
                series = self._series_of_market(ticker)
            try:
                body = self._candle_request(ticker, series if not historical else None, params)
            except NotFound:
                if switched:
                    raise
                switched = True
                historical = not historical
                self._settled_at.pop(ticker, None)
                continue
            for candle in parse_candles(body, ticker, interval_minutes):
                if start < candle.end_ts <= end:
                    candles[candle.end_ts] = candle
            a = b
        return [candles[k] for k in sorted(candles)]

    def liquidity_dollars(self, ticker: str, side: Side) -> Decimal:
        """Dollars offered on ``side`` within ``config.caps.liquidity_band`` of its best ask."""
        book = self.get_order_book(ticker)
        return book.resting_dollars_near_touch(side, self.config.caps.liquidity_band)

    # -- for the snapshot builder and the outcome resolver

    def historical_cutoff(self) -> HistoricalCutoff:
        """The current cutoffs, cached for five minutes of the clock's time."""
        now = self.clock.now()
        if self._cutoff is not None and now - self._cutoff[0] < CUTOFF_TTL:
            return self._cutoff[1]
        cutoff = parse_cutoff(self.client.get("/historical/cutoff"))
        self._cutoff = (now, cutoff)
        return cutoff

    def iter_series_events(
        self, series_ticker: str, *, closing_from: datetime, closing_to: datetime
    ) -> Iterator[Event]:
        """Every event of a series whose close time is in ``[closing_from, closing_to]``.

        Both sides of the cutoff are read, so settled events come with their
        markets, rules and results. ``closing_to`` is clamped to the clock's
        time. An event's close time is its latest market close.

        1. Live: page through ``GET /events?series_ticker=...&with_nested_markets
           =true&min_close_ts=...`` (events with a market closing at or after
           ``closing_from``). Archived events are listed too, without markets.
        2. Historical: page once through ``GET /historical/markets
           ?series_ticker=...`` and group the markets by event.
        3. Merge per event (live objects win on a shared ticker). An archived
           event whose markets close in the window but which the live list
           left out is fetched with ``GET /events/{ticker}``.
        4. Yield the events whose close time is in the window, earliest first.
        """
        closing_to = min(closing_to, self.clock.now())
        if closing_to < closing_from:
            return
        params = {
            "series_ticker": series_ticker,
            "with_nested_markets": True,
            "min_close_ts": unix_seconds(closing_from) - 1,
            "limit": EVENTS_PAGE_LIMIT,
        }
        raw_events = {
            e["event_ticker"]: e for e in self.client.paginate("/events", params, "events")
        }
        historical = self._historical_markets_by_event(series_ticker)
        for event_ticker, archived in historical.items():
            latest = max(m.close_time for m in archived)
            if event_ticker not in raw_events and closing_from <= latest <= closing_to:
                body = self.client.get(f"/events/{event_ticker}")
                raw_events[event_ticker] = body["event"]
        events = []
        for event_ticker, raw in raw_events.items():
            live = [parse_market(m) for m in raw.get("markets") or []]
            markets = merge_markets(live, historical.get(event_ticker, []))
            if not markets:
                continue
            event = parse_event(raw, markets)
            if closing_from <= event.close_time <= closing_to:
                events.append(event)
        yield from sorted(events, key=lambda e: (e.close_time, e.event_ticker))

    # -- internals

    def _historical_markets_by_event(self, series_ticker: str) -> dict[str, list[Market]]:
        grouped: dict[str, list[Market]] = defaultdict(list)
        params = {"series_ticker": series_ticker, "limit": MARKETS_PAGE_LIMIT}
        for raw in self.client.paginate("/historical/markets", params, "markets"):
            market = parse_market(raw)
            grouped[market.event_ticker].append(market)
        return dict(grouped)

    def _is_historical(self, ticker: str) -> bool:
        if ticker not in self._settled_at:
            self.get_market(ticker)
        settled_at = self._settled_at.get(ticker)
        if settled_at is None:
            return False
        return settled_at < self.historical_cutoff().market_settled_ts

    def _series_of_market(self, ticker: str) -> str:
        event_ticker = self._market_event.get(ticker) or self.get_market(ticker).event_ticker
        series = self._event_series.get(event_ticker)
        if series is None:
            body = self.client.get(f"/events/{event_ticker}")
            series = body["event"]["series_ticker"]
            self._event_series[event_ticker] = series
        return series

    def _candle_request(
        self, ticker: str, series: str | None, params: dict[str, int]
    ) -> dict[str, Any]:
        """The live endpoint when a series is given, else the historical one."""
        if series is None:
            return self.client.get(f"/historical/markets/{ticker}/candlesticks", params)
        return self.client.get(f"/series/{series}/markets/{ticker}/candlesticks", params)


__all__ = ["KalshiData"]
