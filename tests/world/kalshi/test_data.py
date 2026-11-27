"""The read-only data adapter at the Exchange port: live plus historical merges, candles, liquidity."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from pmfa.errors import NotFound
from pmfa.ports import Exchange
from pmfa.records.market import MarketStatus, Side
from pmfa.world.kalshi.data import KalshiData
from tests.world.kalshi.fakes import NOW, FakeKalshi, client, clock, config, fixture

EMPTY_MARKETS = (200, {"markets": [], "cursor": ""})


def data(fake: FakeKalshi, at: datetime = NOW) -> KalshiData:
    return KalshiData(client(fake), clock(at), config())


def market_reply(event_fixture: str, ticker: str) -> tuple[int, dict]:
    """A ``GET /markets/{ticker}`` body built from a recorded event's nested market."""
    _, body = fixture(event_fixture)
    for market in body["event"]["markets"]:
        if market["ticker"] == ticker:
            return 200, {"market": market}
    raise KeyError(ticker)


def candle_params(request: httpx.Request) -> tuple[int, int, int]:
    q = request.url.params
    return int(q["start_ts"]), int(q["end_ts"]), int(q["period_interval"])


def test_it_is_an_exchange() -> None:
    assert isinstance(data(FakeKalshi()), Exchange)


def test_list_open_events_pages_to_the_end_with_nested_markets() -> None:
    _, p1 = fixture("events_open_p1")
    fake = FakeKalshi()
    fake.add("GET", "/events", "events_open_p2", status="open", cursor=p1["cursor"])
    fake.add("GET", "/events", "events_open_p1", status="open")
    events = data(fake).list_events("KXFEDDECISION")
    assert [e.event_ticker for e in events] == [
        "KXFEDDECISION-26DEC",
        "KXFEDDECISION-27JAN",
        "KXFEDDECISION-27MAR",
        "KXFEDDECISION-27APR",
    ]
    assert all(len(e.markets) == 2 for e in events)
    assert all(m.status is MarketStatus.OPEN for e in events for m in e.markets)
    first = fake.requests[0].url.params
    assert (first["status"], first["with_nested_markets"], first["limit"]) == (
        "open",
        "true",
        "200",
    )
    assert not fake.sent("GET", "/historical/markets")


def test_list_events_with_closed_merges_archived_markets() -> None:
    fake = FakeKalshi()
    fake.add("GET", "/events", "events_window")
    fake.add("GET", "/historical/markets", "hist_markets_series")
    events = {
        e.event_ticker: e for e in data(fake).list_events("KXFEDDECISION", include_closed=True)
    }
    assert set(events) == {
        "KXFEDDECISION-26JUL",
        "KXFEDDECISION-26SEP",
        "KXFEDDECISION-26OCT",
        "KXFEDDECISION-26DEC",
    }
    archived = events["KXFEDDECISION-26SEP"]
    assert len(archived.markets) == 2
    assert all(m.status is MarketStatus.SETTLED and m.result is not None for m in archived.markets)
    assert "status" not in fake.requests[0].url.params


def test_get_event_behind_the_cutoff_reads_historical_markets() -> None:
    fake = FakeKalshi()
    fake.add("GET", "/events/KXFEDDECISION-26SEP", "event_26SEP")
    fake.add(
        "GET", "/historical/markets", "hist_markets_event_26SEP", event_ticker="KXFEDDECISION-26SEP"
    )
    event = data(fake).get_event("KXFEDDECISION-26SEP")
    assert event.series_ticker == "KXFEDDECISION"
    assert {m.ticker: m.result for m in event.markets} == {
        "KXFEDDECISION-26SEP-H0": Side.NO,
        "KXFEDDECISION-26SEP-C25": Side.NO,
    }
    assert event.close_time == datetime(2026, 9, 16, 17, 59, tzinfo=UTC)


def test_get_open_event_does_not_touch_the_historical_side() -> None:
    _, page = fixture("events_open_p1")
    raw = page["events"][0]
    fake = FakeKalshi().add(
        "GET", f"/events/{raw['event_ticker']}", (200, {"event": raw, "markets": []})
    )
    event = data(fake).get_event(raw["event_ticker"])
    assert len(event.markets) == 2
    assert not fake.sent("GET", "/historical/markets")


def test_get_settled_live_event_checks_the_historical_side_too() -> None:
    fake = FakeKalshi()
    fake.add("GET", "/events/KXFEDDECISION-26OCT", "event_26OCT")
    fake.add("GET", "/historical/markets", EMPTY_MARKETS, event_ticker="KXFEDDECISION-26OCT")
    event = data(fake).get_event("KXFEDDECISION-26OCT")
    assert {m.ticker for m in event.markets} == {
        "KXFEDDECISION-26OCT-H0",
        "KXFEDDECISION-26OCT-C25",
    }
    assert len(fake.sent("GET", "/historical/markets")) == 1


def test_unknown_event_is_not_found() -> None:
    fake = FakeKalshi().add("GET", "/events/KXNOPE", "market_26SEP_H0_live_404")
    with pytest.raises(NotFound):
        data(fake).get_event("KXNOPE")


def test_get_market_falls_back_to_historical_on_a_live_404() -> None:
    fake = FakeKalshi()
    fake.add("GET", "/markets/KXFEDDECISION-26SEP-H0", "market_26SEP_H0_live_404")
    fake.add("GET", "/historical/markets/KXFEDDECISION-26SEP-H0", "hist_market_26SEP_H0")
    market = data(fake).get_market("KXFEDDECISION-26SEP-H0")
    assert market.status is MarketStatus.SETTLED
    assert market.result is Side.NO


def test_live_market_candles_come_from_the_series_endpoint() -> None:
    ticker = "KXFEDDECISION-26OCT-H0"
    fake = FakeKalshi()
    fake.add("GET", f"/markets/{ticker}", market_reply("event_26OCT", ticker))
    fake.add("GET", "/historical/cutoff", "cutoff")
    fake.add("GET", "/events/KXFEDDECISION-26OCT", "event_26OCT")
    fake.add(
        "GET", f"/series/KXFEDDECISION/markets/{ticker}/candlesticks", "candles_live_26OCT_H0_60"
    )
    start = datetime(2026, 10, 28, 12, tzinfo=UTC)
    end = datetime(2026, 10, 28, 20, tzinfo=UTC)
    candles = data(fake).get_candles(ticker, start=start, end=end, interval_minutes=60)
    # The recorded response starts with the candle ending at `start`, which (start, end] excludes.
    assert [c.end_ts.hour for c in candles] == [13, 14, 15, 16, 17, 18]
    (request,) = fake.sent("GET", f"/series/KXFEDDECISION/markets/{ticker}/candlesticks")
    assert candle_params(request) == (int(start.timestamp()) + 1, int(end.timestamp()), 60)
    assert not fake.sent("GET", f"/historical/markets/{ticker}/candlesticks")


def test_archived_market_candles_come_from_the_historical_endpoint() -> None:
    ticker = "KXFEDDECISION-26SEP-H0"
    fake = FakeKalshi()
    fake.add("GET", f"/markets/{ticker}", "market_26SEP_H0_live_404")
    fake.add("GET", f"/historical/markets/{ticker}", "hist_market_26SEP_H0")
    fake.add("GET", "/historical/cutoff", "cutoff")
    fake.add("GET", f"/historical/markets/{ticker}/candlesticks", "candles_hist_26SEP_H0_60")
    adapter = data(fake)
    start = datetime(2026, 9, 16, 11, tzinfo=UTC)
    end = datetime(2026, 9, 16, 20, tzinfo=UTC)
    candles = adapter.get_candles(ticker, start=start, end=end, interval_minutes=60)
    assert len(candles) == 7
    assert candles[0].price is not None and candles[0].price.close == Decimal("0.1200")
    adapter.get_candles(ticker, start=start, end=end, interval_minutes=60)
    assert len(fake.sent("GET", "/historical/cutoff")) == 1  # cached
    assert len(fake.sent("GET", f"/historical/markets/{ticker}")) == 1  # routing remembered


def test_long_ranges_are_chunked_to_5000_periods_and_clamped_to_the_clock() -> None:
    ticker = "KXFEDDECISION-26DEC-H0"
    fake = FakeKalshi()
    fake.add("GET", f"/markets/{ticker}", "market_26DEC_H0")
    fake.add("GET", "/historical/cutoff", "cutoff")
    fake.add(
        "GET",
        "/events/KXFEDDECISION-26DEC",
        (200, {"event": fixture("events_open_p1")[1]["events"][0]}),
    )
    path = f"/series/KXFEDDECISION/markets/{ticker}/candlesticks"
    fake.add("GET", path, (200, {"ticker": ticker, "candlesticks": []}))
    start = NOW - timedelta(days=10)
    data(fake).get_candles(ticker, start=start, end=NOW + timedelta(days=5), interval_minutes=1)
    windows = [candle_params(r) for r in fake.sent("GET", path)]
    assert len(windows) == 3
    assert windows[0][0] == int(start.timestamp()) + 1
    assert windows[-1][1] == int(NOW.timestamp())
    for (a, b, interval), nxt in zip(windows, [*windows[1:], None], strict=True):
        assert interval == 1
        assert (b - (a - 1)) // 60 <= 5000
        if nxt is not None:
            assert nxt[0] == b + 1  # contiguous, no overlap


def test_a_moved_cutoff_switches_to_the_historical_endpoint_once() -> None:
    ticker = "KXFEDDECISION-26DEC-H0"
    fake = FakeKalshi()
    fake.add("GET", f"/markets/{ticker}", "market_26DEC_H0")
    fake.add("GET", "/historical/cutoff", "cutoff")
    fake.add(
        "GET", f"/series/KXFEDDECISION/markets/{ticker}/candlesticks", "market_26SEP_H0_live_404"
    )
    fake.add(
        "GET",
        f"/historical/markets/{ticker}/candlesticks",
        (200, {"ticker": ticker, "candlesticks": []}),
    )
    adapter = data(fake)
    candles = adapter.get_candles(
        ticker,
        start=NOW - timedelta(hours=3),
        end=NOW,
        interval_minutes=60,
        series_ticker="KXFEDDECISION",
    )
    assert candles == []
    assert len(fake.sent("GET", f"/historical/markets/{ticker}/candlesticks")) == 1


def test_empty_window_makes_no_request() -> None:
    fake = FakeKalshi()
    assert (
        data(fake).get_candles("X", start=NOW, end=NOW + timedelta(hours=1), interval_minutes=60)
        == []
    )
    assert fake.requests == []


def test_order_book_and_liquidity_near_the_touch() -> None:
    fake = FakeKalshi().add(
        "GET", "/markets/KXFEDDECISION-26DEC-H0/orderbook", "orderbook_26DEC_H0"
    )
    adapter = data(fake)
    book = adapter.get_order_book("KXFEDDECISION-26DEC-H0")
    assert book.as_of == NOW
    # YES asks are 1 - NO bids: 0.07, 0.08, 0.09; the 2-cent band from 0.07 reaches 0.09.
    expected = (
        Decimal("0.07") * Decimal("85496.82")
        + Decimal("0.08") * Decimal("5050.32")
        + Decimal("0.09") * Decimal("54255.24")
    )
    assert adapter.liquidity_dollars("KXFEDDECISION-26DEC-H0", Side.YES) == expected


def test_iter_series_events_merges_both_sides_within_the_window() -> None:
    _, sep = fixture("event_26SEP")
    jun = copy.deepcopy(sep)  # the 26SEP event object, renamed: the live list omits 26JUN here
    jun["event"]["event_ticker"] = "KXFEDDECISION-26JUN"
    fake = FakeKalshi()
    fake.add("GET", "/events", "events_window")
    fake.add("GET", "/historical/markets", "hist_markets_series", series_ticker="KXFEDDECISION")
    fake.add("GET", "/events/KXFEDDECISION-26JUN", (200, jun))
    events = list(
        data(fake).iter_series_events(
            "KXFEDDECISION",
            closing_from=datetime(2026, 7, 1, tzinfo=UTC),
            closing_to=datetime(2026, 12, 31, tzinfo=UTC),  # clamped to the clock (Nov 25)
        )
    )
    assert [e.event_ticker for e in events] == [
        "KXFEDDECISION-26JUL",
        "KXFEDDECISION-26SEP",
        "KXFEDDECISION-26OCT",
    ]
    for event in events:
        assert len(event.markets) == 2
        assert all(m.result is not None and m.rules_primary for m in event.markets)
    assert fake.requests[0].url.params["min_close_ts"] == str(1782864000 - 1)
