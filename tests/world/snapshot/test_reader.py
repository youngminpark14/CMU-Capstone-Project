"""SnapshotExchange: every as-of property, the synthesised book and the liquidity proxy."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pmfa.config import Config
from pmfa.errors import NotFound
from pmfa.ports import Exchange
from pmfa.records.market import MarketStatus, Side
from pmfa.world.clocks import SimClock
from pmfa.world.snapshot.reader import SnapshotExchange
from pmfa.world.snapshot.store import SnapshotStore
from tests.world.snapshot.fakes import (
    AUG,
    FED,
    JUL,
    JUL_CLOSE,
    JUL_SETTLED,
    T0,
    A,
    C,
    D,
    utc,
)

M = timedelta(minutes=1)


def at(store: SnapshotStore, config: Config, moment: datetime) -> SnapshotExchange:
    return SnapshotExchange(store, SimClock(moment), config)


def test_is_an_exchange(store: SnapshotStore, config: Config) -> None:
    assert isinstance(at(store, config, T0), Exchange)


@pytest.mark.parametrize(
    ("moment", "status"),
    [
        (utc(2026, 6, 10), MarketStatus.UNOPENED),
        (T0, MarketStatus.OPEN),
        (JUL_CLOSE + 5 * M, MarketStatus.CLOSED),
        (JUL_SETTLED, MarketStatus.SETTLED),
    ],
)
def test_status_follows_the_clock(
    store: SnapshotStore, config: Config, moment: datetime, status: MarketStatus
) -> None:
    assert at(store, config, moment).get_market(A).status is status


def test_result_is_hidden_until_settlement(store: SnapshotStore, config: Config) -> None:
    for moment in (T0, JUL_CLOSE, JUL_SETTLED - timedelta(seconds=1)):
        market = at(store, config, moment).get_market(A)
        assert market.result is None
        assert market.settled_at is None
    settled = at(store, config, JUL_SETTLED).get_market(A)
    assert settled.result is Side.YES
    assert settled.settled_at == JUL_SETTLED


def test_an_unsettled_market_stays_closed(store: SnapshotStore, config: Config) -> None:
    market = at(store, config, utc(2026, 11, 1)).get_market(C)
    assert market.status is MarketStatus.CLOSED
    assert market.result is None


def test_quotes_never_come_from_a_later_candle(store: SnapshotStore, config: Config) -> None:
    # Before the first minute candle ends (12:01), the 12:00 hourly candle is the latest.
    before = at(store, config, T0 + timedelta(seconds=59)).get_market(A)
    assert (before.yes_bid, before.yes_ask, before.last_price) == (
        Decimal("0.38"),
        Decimal("0.42"),
        Decimal("0.40"),
    )
    # At 12:01:30 the 12:01 minute candle is the latest; 12:02's must not show.
    during = at(store, config, T0 + 90 * timedelta(seconds=1)).get_market(A)
    assert (during.yes_bid, during.yes_ask, during.last_price) == (
        Decimal("0.41"),
        Decimal("0.45"),
        Decimal("0.43"),
    )
    assert during.open_interest == Decimal("1000")
    # Exactly at a candle's end time it counts.
    exact = at(store, config, T0 + 2 * M).get_market(A)
    assert exact.yes_bid == Decimal("0.40")


def test_the_final_record_quotes_are_not_shown(store: SnapshotStore, config: Config) -> None:
    # The stored final record says 0.99 / 1.00 / 9999 contracts; none of it leaks.
    market = at(store, config, T0 + 31 * M).get_market(A)
    assert market.yes_bid == Decimal("0.50")
    assert market.volume == Decimal("580")
    early = at(store, config, utc(2026, 7, 1)).get_market(A)
    assert early.yes_bid is None and early.last_price is None
    assert early.volume == 0


def test_cumulative_volume_counts_each_trade_once(store: SnapshotStore, config: Config) -> None:
    # Hourly 400 through 12:00, then minute candles 100 + 50 + 30 through 12:31.
    assert at(store, config, T0 + 31 * M).get_market(A).volume == Decimal("580")
    # After 13:00 the hourly candle (187) replaces the minutes it covers.
    assert at(store, config, T0 + 65 * M).get_market(A).volume == Decimal("587")
    assert at(store, config, T0 + 65 * M).get_market(A).volume_24h == Decimal("587")


def test_list_events_never_shows_an_event_not_yet_open(
    store: SnapshotStore, config: Config
) -> None:
    assert [e.event_ticker for e in at(store, config, T0).list_events(FED)] == [JUL]
    assert [
        e.event_ticker for e in at(store, config, T0).list_events(FED, include_closed=True)
    ] == [JUL]
    later = at(store, config, utc(2026, 7, 26))
    assert [e.event_ticker for e in later.list_events(FED)] == [JUL, AUG]
    after_close = at(store, config, utc(2026, 7, 30))
    assert [e.event_ticker for e in after_close.list_events(FED)] == [AUG]
    assert [e.event_ticker for e in after_close.list_events(FED, include_closed=True)] == [
        JUL,
        AUG,
    ]
    assert at(store, config, utc(2026, 6, 1)).list_events(FED, include_closed=True) == []


def test_event_views_leave_out_markets_not_yet_open(store: SnapshotStore, config: Config) -> None:
    with pytest.raises(NotFound):
        at(store, config, T0).get_event(AUG)
    event = at(store, config, utc(2026, 7, 26)).get_event(AUG)
    assert [m.ticker for m in event.markets] == [C]
    full = at(store, config, utc(2026, 8, 2)).get_event(AUG)
    assert sorted(m.ticker for m in full.markets) == sorted([C, D])
    # By ticker, a market not yet open answers with its status.
    assert at(store, config, utc(2026, 7, 26)).get_market(D).status is MarketStatus.UNOPENED


def test_get_candles_clamps_end_to_the_clock(store: SnapshotStore, config: Config) -> None:
    exchange = at(store, config, T0 + 150 * timedelta(seconds=1))
    minutes = exchange.get_candles(
        A, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=2), interval_minutes=1
    )
    assert [c.end_ts for c in minutes] == [T0 + M, T0 + 2 * M]
    hours = exchange.get_candles(
        A, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=2), interval_minutes=60
    )
    assert [c.end_ts for c in hours] == [T0]
    assert (
        exchange.get_candles(
            A, start=T0 + timedelta(hours=1), end=T0 + timedelta(hours=2), interval_minutes=1
        )
        == []
    )
    with pytest.raises(ValueError):
        exchange.get_candles(A, start=T0, end=T0, interval_minutes=1440)


def test_order_book_and_liquidity_worked_example(store: SnapshotStore, config: Config) -> None:
    # Clock 12:31. Latest candle: the 12:30 minute (bid 0.50, ask 0.52).
    # Trailing 60 minutes (11:31, 12:31]: 100 + 50 + 30 = 180 contracts; 10% -> 18.
    exchange = at(store, config, T0 + 31 * M)
    book = exchange.get_order_book(A)
    assert book.as_of == T0 + 31 * M
    assert [(lvl.price, lvl.contracts) for lvl in book.yes_bids] == [(Decimal("0.50"), 18)]
    assert [(lvl.price, lvl.contracts) for lvl in book.no_bids] == [(Decimal("0.48"), 18)]
    assert book.best_ask(Side.YES).price == Decimal("0.52")
    # Dollars traded, on each side's scale: YES 100*0.43 + 50*0.44 + 30*0.51 = 80.30;
    # NO 100*0.57 + 50*0.56 + 30*0.49 = 99.70.
    assert exchange.liquidity_dollars(A, Side.YES) == Decimal("80.30")
    assert exchange.liquidity_dollars(A, Side.NO) == Decimal("99.70")


def test_order_book_is_empty_without_recent_trades_or_when_closed(
    store: SnapshotStore, config: Config
) -> None:
    quiet = at(store, config, T0 + timedelta(hours=3)).get_order_book(A)
    assert quiet.yes_bids == [] and quiet.no_bids == []
    closed = at(store, config, JUL_CLOSE + M).get_order_book(A)
    assert closed.yes_bids == [] and closed.no_bids == []
    assert at(store, config, T0 + timedelta(hours=3)).liquidity_dollars(A, Side.YES) == 0


def test_unknown_tickers_raise_not_found(store: SnapshotStore, config: Config) -> None:
    exchange = at(store, config, T0)
    for call in (
        lambda: exchange.get_market("NOPE"),
        lambda: exchange.get_event("NOPE"),
        lambda: exchange.get_series("NOPE"),
        lambda: exchange.get_order_book("NOPE"),
        lambda: exchange.liquidity_dollars("NOPE", Side.YES),
        lambda: exchange.get_candles("NOPE", start=T0 - M, end=T0, interval_minutes=1),
    ):
        with pytest.raises(NotFound):
            call()
