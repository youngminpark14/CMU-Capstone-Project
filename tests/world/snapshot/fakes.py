"""A small invented exchange for the snapshot tests: two Fed-style events, a handful of candles.

Times are chosen so every as-of rule has a case on both sides of it:

- ``26JUL`` (markets A and B) opens 2026-06-15, closes 2026-07-29 18:00 and
  settles 18:10 with A = YES.
- ``26AUG`` opens later: market C on 2026-07-25, market D (a strike added
  later) on 2026-08-01; it closes 2026-08-10 and is not settled.
- Market A has one-minute candles at 12:01, 12:02, 12:30 and 12:40 on
  2026-07-20 and hourly candles ending 12:00 and 13:00 that day.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pmfa.records.market import OHLC, Candle, Event, Market, MarketStatus, Series, Side
from pmfa.world.kalshi.parse import HistoricalCutoff

FED = "KXFEDDECISION"
CPI = "KXCPIYOY"
JUL = "KXFEDDECISION-26JUL"
AUG = "KXFEDDECISION-26AUG"
A, B = f"{JUL}-H0", f"{JUL}-C25"
C, D = f"{AUG}-H0", f"{AUG}-C25"

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
JUL_CLOSE = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
JUL_SETTLED = datetime(2026, 7, 29, 18, 10, tzinfo=UTC)
AUG_CLOSE = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
BUILT = datetime(2026, 11, 25, tzinfo=UTC)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def ohlc(o: str, h: str, low: str, c: str) -> OHLC:
    return OHLC(open=Decimal(o), high=Decimal(h), low=Decimal(low), close=Decimal(c))


def flat(p: str) -> OHLC:
    return ohlc(p, p, p, p)


def candle(
    ticker: str,
    end: datetime,
    interval: int,
    *,
    bid: OHLC | None,
    ask: OHLC | None,
    price: OHLC | None,
    volume: str,
    oi: str | None = None,
) -> Candle:
    return Candle(
        ticker=ticker,
        end_ts=end,
        interval_minutes=interval,
        yes_bid=bid,
        yes_ask=ask,
        price=price,
        volume=Decimal(volume),
        open_interest=None if oi is None else Decimal(oi),
    )


def market(
    ticker: str,
    event: str,
    *,
    opens: datetime,
    closes: datetime,
    settled: datetime | None = None,
    result: Side | None = None,
) -> Market:
    """A market as the exchange returns it at build time (final status, result, quotes)."""
    return Market(
        ticker=ticker,
        event_ticker=event,
        title=f"{ticker} title",
        subtitle=ticker.rsplit("-", 1)[1],
        status=MarketStatus.SETTLED if settled else MarketStatus.OPEN,
        open_time=opens,
        close_time=closes,
        strike_type="custom",
        rules_primary=f"Resolves YES if {ticker} happens.",
        yes_bid=Decimal("0.99") if result is Side.YES else Decimal("0.01"),
        yes_ask=Decimal("1"),
        last_price=Decimal("0.99"),
        volume=Decimal("9999"),
        result=result,
        settled_at=settled,
    )


def events() -> list[Event]:
    jul = Event(
        event_ticker=JUL,
        series_ticker=FED,
        title="Fed decision in July 2026",
        mutually_exclusive=True,
        markets=[
            market(
                A, JUL, opens=utc(2026, 6, 15, 14), closes=JUL_CLOSE, settled=JUL_SETTLED,
                result=Side.YES,
            ),
            market(
                B, JUL, opens=utc(2026, 6, 15, 14), closes=JUL_CLOSE, settled=JUL_SETTLED,
                result=Side.NO,
            ),
        ],
    )
    aug = Event(
        event_ticker=AUG,
        series_ticker=FED,
        title="Fed decision in August 2026",
        mutually_exclusive=True,
        markets=[
            market(C, AUG, opens=utc(2026, 7, 25), closes=AUG_CLOSE),
            market(D, AUG, opens=utc(2026, 8, 1), closes=AUG_CLOSE),
        ],
    )
    return [jul, aug]


def minute_candles_a() -> list[Candle]:
    return [
        candle(
            A, T0 + timedelta(minutes=1), 1,
            bid=ohlc("0.40", "0.42", "0.39", "0.41"), ask=ohlc("0.44", "0.46", "0.43", "0.45"),
            price=ohlc("0.42", "0.43", "0.42", "0.43"), volume="100", oi="1000",
        ),
        candle(
            A, T0 + timedelta(minutes=2), 1,
            bid=ohlc("0.41", "0.41", "0.40", "0.40"), ask=ohlc("0.45", "0.47", "0.45", "0.46"),
            price=flat("0.44"), volume="50", oi="1050",
        ),
        candle(
            A, T0 + timedelta(minutes=30), 1,
            bid=flat("0.50"), ask=flat("0.52"), price=flat("0.51"), volume="30", oi="1080",
        ),
        candle(
            A, T0 + timedelta(minutes=40), 1,
            bid=flat("0.50"), ask=flat("0.52"), price=flat("0.51"), volume="7", oi="1087",
        ),
    ]


def hourly_candles_a() -> list[Candle]:
    return [
        candle(
            A, T0, 60, bid=flat("0.38"), ask=flat("0.42"), price=flat("0.40"), volume="400",
            oi="900",
        ),
        candle(
            A, T0 + timedelta(hours=1), 60,
            bid=ohlc("0.40", "0.50", "0.39", "0.50"), ask=ohlc("0.44", "0.52", "0.43", "0.52"),
            price=ohlc("0.42", "0.51", "0.42", "0.51"), volume="187", oi="1087",
        ),
    ]


def candles() -> dict[tuple[str, int], list[Candle]]:
    """Every invented candle, by (ticker, interval)."""
    out = {(A, 1): minute_candles_a(), (A, 60): hourly_candles_a()}
    for ticker in (B, C, D):
        out[(ticker, 60)] = [
            candle(
                ticker, utc(2026, 7, 26, 15), 60, bid=flat("0.10"), ask=flat("0.12"),
                price=flat("0.11"), volume="5",
            )
        ]
        out[(ticker, 1)] = [
            candle(
                ticker, utc(2026, 7, 26, 14, 30), 1, bid=flat("0.10"), ask=flat("0.12"),
                price=flat("0.11"), volume="5",
            )
        ]
    return out


class Interrupted(RuntimeError):
    """Raised by the fake exchange to simulate a build that stops part way."""


@dataclass
class FakeExchange:
    """The reading methods of ``KalshiData`` over the invented data above."""

    fail_after: int | None = None
    """Raise :class:`Interrupted` on this many-plus-one-th candle request."""
    candle_calls: list[tuple[str, int, datetime, datetime]] = field(default_factory=list)
    event_calls: list[tuple[str, datetime, datetime]] = field(default_factory=list)

    def historical_cutoff(self) -> HistoricalCutoff:
        return HistoricalCutoff(market_settled_ts=utc(2026, 9, 26))

    def get_series(self, series_ticker: str) -> Series:
        return Series(
            ticker=series_ticker,
            title=f"{series_ticker} series",
            category="Economics",
            fee_type="quadratic_with_maker_fees",
            fee_multiplier=Decimal("1"),
        )

    def iter_series_events(
        self, series_ticker: str, *, closing_from: datetime, closing_to: datetime
    ) -> Iterator[Event]:
        self.event_calls.append((series_ticker, closing_from, closing_to))
        for event in events():
            if event.series_ticker == series_ticker and (
                closing_from <= event.close_time <= closing_to
            ):
                yield event

    def get_candles(
        self, ticker: str, *, start: datetime, end: datetime, interval_minutes: int
    ) -> list[Candle]:
        if self.fail_after is not None and len(self.candle_calls) >= self.fail_after:
            raise Interrupted(f"interrupted before {ticker} {interval_minutes}m")
        self.candle_calls.append((ticker, interval_minutes, start, end))
        return [
            c for c in candles().get((ticker, interval_minutes), []) if start < c.end_ts <= end
        ]

