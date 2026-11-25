"""Wire JSON to records, on recorded responses and on responses built from the OpenAPI schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmfa.errors import ExchangeError
from pmfa.records.market import MarketStatus, Side
from pmfa.world.kalshi.parse import (
    count,
    event_markets,
    market_status,
    parse_balance,
    parse_candles,
    parse_cutoff,
    parse_event,
    parse_fill,
    parse_market,
    parse_orderbook,
    parse_positions,
    parse_series,
    price,
    result_side,
    utc,
)
from tests.world.kalshi.fakes import NOW, fixture


def body(name: str):
    return fixture(name)[1]


def test_fixed_point_strings_become_exact_decimals() -> None:
    assert price("0.0500") == Decimal("0.0500")
    assert price("0.123456") == Decimal("0.123456")
    assert price(None) is None
    assert count("14219.15") == Decimal("14219.15")
    assert count("0.00") == Decimal("0")
    assert count(None) == Decimal("0")
    with pytest.raises(ExchangeError):
        count("12,5")


def test_times_are_utc() -> None:
    assert utc("2026-12-09T18:59:00Z") == datetime(2026, 12, 9, 18, 59, tzinfo=UTC)
    assert utc("2026-10-27T18:37:20.014572Z").microsecond == 14572
    assert utc(1793188800) == datetime(2026, 10, 28, 12, tzinfo=UTC)
    assert utc("") is None
    with pytest.raises(ExchangeError):
        utc("2026-12-09T18:59:00")


@pytest.mark.parametrize(
    ("word", "status"),
    [
        ("initialized", MarketStatus.UNOPENED),
        ("active", MarketStatus.OPEN),
        ("inactive", MarketStatus.PAUSED),
        ("closed", MarketStatus.CLOSED),
        ("determined", MarketStatus.CLOSED),
        ("disputed", MarketStatus.CLOSED),
        ("amended", MarketStatus.CLOSED),
        ("finalized", MarketStatus.SETTLED),
    ],
)
def test_status_words_map_to_the_life_cycle(word: str, status: MarketStatus) -> None:
    assert market_status(word) is status


def test_unknown_status_is_an_error() -> None:
    with pytest.raises(ExchangeError):
        market_status("halted")


def test_result_words() -> None:
    assert result_side("") is None
    assert result_side("yes") is Side.YES
    assert result_side("no") is Side.NO
    assert result_side("scalar") is None


def test_series_reads_fee_fields() -> None:
    series = parse_series(body("series_KXFEDDECISION"))
    assert series.ticker == "KXFEDDECISION"
    assert series.fee_type == "quadratic_with_maker_fees"
    assert series.fee_multiplier == Decimal("1")
    assert series.settlement_sources[0].url == "https://www.federalreserve.gov"


def test_open_market_with_fractional_counts() -> None:
    market = parse_market(body("market_26DEC_H0")["market"])
    assert market.status is MarketStatus.OPEN
    assert market.subtitle == "Fed maintains rate"
    assert market.custom_strike == {"Hike": "0"}
    assert market.result is None and market.settled_at is None
    assert market.close_time == datetime(2026, 12, 9, 18, 59, tzinfo=UTC)
    assert market.price_ranges[0].step == Decimal("0.0100")
    assert market.volume % 1 != 0  # lifetime volume is fractional on this market
    assert market.yes_bid is not None and market.yes_ask is not None
    assert market.yes_bid < market.yes_ask


def test_settled_historical_market_has_result_and_settlement_time() -> None:
    market = parse_market(body("hist_market_26SEP_H0")["market"])
    assert market.status is MarketStatus.SETTLED
    assert market.result is Side.NO  # recorded: the Fed did not hold in September 2026
    assert market.settled_at is not None and market.settled_at.tzinfo is UTC
    assert market.rules_primary.startswith("If the Federal Reserve")


def test_event_markets_come_from_whichever_list_is_filled() -> None:
    nested = body("event_26OCT")
    markets = event_markets(nested)
    assert {m.ticker for m in markets} == {"KXFEDDECISION-26OCT-H0", "KXFEDDECISION-26OCT-C25"}
    assert event_markets(body("event_26SEP")) == []
    event = parse_event(nested["event"], markets)
    assert event.series_ticker == "KXFEDDECISION"
    assert event.mutually_exclusive


def test_order_book_levels_are_reversed_to_best_first() -> None:
    book = parse_orderbook(body("orderbook_26DEC_H0"), "KXFEDDECISION-26DEC-H0", NOW)
    yes_prices = [lvl.price for lvl in book.yes_bids]
    assert yes_prices == sorted(yes_prices, reverse=True)
    assert book.best_bid(Side.YES).price == Decimal("0.0500")
    # The best NO bid at 0.93 is an offer to sell YES at 0.07.
    assert book.best_ask(Side.YES).price == Decimal("0.0700")
    assert book.yes_bids[0].contracts == Decimal("171.78")


def test_empty_order_book() -> None:
    book = parse_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, "X", NOW)
    assert book.best_bid(Side.YES) is None and book.best_ask(Side.NO) is None


def test_live_candles() -> None:
    candles = parse_candles(body("candles_live_26OCT_H0_60"), "KXFEDDECISION-26OCT-H0", 60)
    first = candles[0]
    assert first.end_ts == datetime(2026, 10, 28, 12, tzinfo=UTC)
    assert first.price is not None and first.price.close == Decimal("0.9600")
    assert first.yes_bid is not None and first.yes_bid.open == Decimal("0.9500")
    assert first.volume == Decimal("2964.30")
    assert first.open_interest == Decimal("4687515.20")


def test_historical_candles_use_unsuffixed_names() -> None:
    candles = parse_candles(body("candles_hist_26SEP_H0_60"), "KXFEDDECISION-26SEP-H0", 60)
    assert candles[0].price is not None and candles[0].price.close == Decimal("0.1200")
    assert candles[0].volume == Decimal("238699.00")


def test_empty_candles_have_no_price_on_either_side_of_the_cutoff() -> None:
    historical = parse_candles(body("candles_hist_26SEP_C26_60"), "KXFEDDECISION-26SEP-C26", 60)
    assert sum(c.price is None for c in historical) == 2
    assert all(c.yes_ask is not None for c in historical)
    live = parse_candles(body("candles_live_27DEC_C26_1"), "KXFEDDECISION-27DEC-C26", 1)
    assert live and all(c.price is None and c.volume == 0 for c in live)


def test_cutoff() -> None:
    cutoff = parse_cutoff(body("cutoff"))
    assert cutoff.market_settled_ts == datetime(2026, 9, 26, tzinfo=UTC)


def test_positions_are_signed_yes_positive_and_carry_their_event() -> None:
    positions = {p.market_ticker: p for p in parse_positions([body("constructed_positions")])}
    assert set(positions) == {"KXFEDDECISION-26DEC-H0", "KXFEDDECISION-26DEC-C25"}  # zero dropped
    no = positions["KXFEDDECISION-26DEC-H0"]
    assert (no.side, no.contracts, no.signed) == (Side.NO, 4, -4)
    assert no.avg_price == Decimal("0.9525")
    assert no.event_ticker == "KXFEDDECISION-26DEC"
    yes = positions["KXFEDDECISION-26DEC-C25"]
    assert (yes.side, yes.contracts, yes.avg_price) == (Side.YES, 2, Decimal("0.2300"))


def test_fills_are_priced_on_their_own_side() -> None:
    fills = [parse_fill(f) for f in body("constructed_fills_partial")["fills"]]
    assert [f.price for f in fills] == [Decimal("0.9500"), Decimal("0.9600")]
    assert all(f.side is Side.NO and not f.simulated for f in fills)
    assert fills[0].contracts == Decimal("3.00")
    assert fills[0].fee == Decimal("0.0100")


def test_balance_equity_is_cash_plus_cost_basis() -> None:
    positions = parse_positions([body("constructed_positions")])
    balance = parse_balance(body("constructed_balance"), positions)
    assert balance.cash == Decimal("995.2500")
    assert balance.equity == Decimal("995.2500") + Decimal("3.8100") + Decimal("0.4600")
