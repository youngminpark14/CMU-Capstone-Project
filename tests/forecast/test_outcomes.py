"""Outcome spaces from real (trimmed) exchange samples."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmfa.forecast import (
    OutcomeSpaceError,
    build_outcome_space,
    event_brief,
    ladder_outcome_for_value,
    outcome_from_results,
)
from pmfa.forecast.outcomes import yes_interval
from pmfa.records.market import Event, Market, MarketStatus, Side

from .scripted import load_event, series

FED_MAPPING = {
    "KXFEDDECISION-26DEC-C26": ["cut_gt25"],
    "KXFEDDECISION-26DEC-C25": ["cut_25"],
    "KXFEDDECISION-26DEC-H0": ["hold"],
    "KXFEDDECISION-26DEC-H25": ["hike_25"],
    "KXFEDDECISION-26DEC-H26": ["hike_gt25"],
}


def test_fed_decision_is_categorical_with_five_stable_options():
    event = load_event("KXFEDDECISION-26DEC")
    space = build_outcome_space(event, series("KXFEDDECISION"))
    assert space.kind == "categorical"
    assert space.outcome_ids() == ["hike_gt25", "hike_25", "hold", "cut_25", "cut_gt25"]
    assert space.market_outcomes == FED_MAPPING


def test_fed_options_are_read_from_the_subtitle_or_ticker_when_custom_strike_is_missing():
    event = load_event("KXFEDDECISION-26DEC", raw_subtitle=True)  # "Hike 0bps", "Cut >25bps"
    stripped = event.model_copy(
        update={"markets": [m.model_copy(update={"custom_strike": {}}) for m in event.markets]}
    )
    assert build_outcome_space(stripped, series("KXFEDDECISION")).market_outcomes == FED_MAPPING
    no_suffix = stripped.model_copy(
        update={
            "markets": [
                m.model_copy(update={"ticker": f"X-{i}"}) for i, m in enumerate(stripped.markets)
            ]
        }
    )
    mapped = build_outcome_space(no_suffix, series("KXFEDDECISION")).market_outcomes
    assert sorted(v[0] for v in mapped.values()) == sorted(v[0] for v in FED_MAPPING.values())


def test_an_unrecognised_fed_market_is_an_error():
    event = load_event("KXFEDDECISION-26DEC")
    odd = event.markets[0].model_copy(
        update={"ticker": "KXFEDDECISION-26DEC-Q9", "custom_strike": {}, "subtitle": "Pause"}
    )
    with pytest.raises(OutcomeSpaceError):
        build_outcome_space(event.model_copy(update={"markets": [odd]}), series("KXFEDDECISION"))


def test_cpi_ladder_bins_and_strict_thresholds():
    event = load_event("KXCPIYOY-26NOV")  # T3.4 ... T3.8, "more than X%"
    space = build_outcome_space(event, series("KXCPIYOY"))
    assert space.kind == "ladder" and space.increment == 0.1
    assert space.outcome_ids() == [
        "le_3.4",
        "gt_3.4_le_3.5",
        "gt_3.5_le_3.6",
        "gt_3.6_le_3.7",
        "gt_3.7_le_3.8",
        "gt_3.8",
    ]
    # "above 3.6" is strict: a reported 3.6 resolves NO, a reported 3.7 YES.
    assert space.market_outcomes["KXCPIYOY-26NOV-T3.6"] == [
        "gt_3.6_le_3.7",
        "gt_3.7_le_3.8",
        "gt_3.8",
    ]
    first, last = space.outcomes[0], space.outcomes[-1]
    assert (first.low, first.high) == (None, 3.4) and (last.low, last.high) == (3.8, None)


@pytest.mark.parametrize(
    ("name", "ticker", "n_bins", "yes_bins"),
    [
        ("KXFED-26DEC", "KXFED-26DEC-T3.50", 4, ["gt_3.50_le_3.75", "gt_3.75"]),
        ("KXPAYROLLS-26NOV", "KXPAYROLLS-26NOV-T50000", 4, ["gt_50000_le_100000", "gt_100000"]),
    ],
)
def test_other_ladders(name, ticker, n_bins, yes_bins):
    event = load_event(name)
    space = build_outcome_space(event, series(event.series_ticker))
    assert len(space.outcomes) == n_bins
    assert space.market_outcomes[ticker] == yes_bins


def _market(ticker: str, strike_type: str, floor: str | None, cap: str | None) -> Market:
    t = datetime(2026, 12, 1, tzinfo=UTC)
    return Market(
        ticker=ticker,
        event_ticker="EV",
        title=ticker,
        status=MarketStatus.OPEN,
        open_time=t,
        close_time=t,
        strike_type=strike_type,
        floor_strike=Decimal(floor) if floor else None,
        cap_strike=Decimal(cap) if cap else None,
    )


def test_strike_types_become_half_open_intervals_of_the_reported_number():
    d = Decimal("0.1")
    assert yes_interval(_market("a", "greater", "3.6", None), d) == (Decimal("3.6"), None)
    assert yes_interval(_market("b", "greater_or_equal", "3.6", None), d) == (Decimal("3.5"), None)
    assert yes_interval(_market("c", "less", None, "3.6"), d) == (None, Decimal("3.5"))
    assert yes_interval(_market("d", "less_or_equal", None, "3.6"), d) == (None, Decimal("3.6"))
    assert yes_interval(_market("e", "between", "3.5", "3.6"), d) == (
        Decimal("3.4"),
        Decimal("3.6"),
    )
    with pytest.raises(OutcomeSpaceError):
        yes_interval(_market("f", "custom", None, None), d)


def test_mixed_strike_types_share_one_set_of_bins():
    markets = [
        _market("EV-GE3.6", "greater_or_equal", "3.6", None),
        _market("EV-LT3.5", "less", None, "3.5"),
        _market("EV-B3.5", "between", "3.5", "3.6"),
    ]
    event = Event(event_ticker="EV", series_ticker="KXCPIYOY", title="t", markets=markets)
    space = build_outcome_space(event, series("KXCPIYOY"))
    assert space.outcome_ids() == ["le_3.4", "gt_3.4_le_3.5", "gt_3.5_le_3.6", "gt_3.6"]
    assert space.market_outcomes["EV-GE3.6"] == ["gt_3.5_le_3.6", "gt_3.6"]
    assert space.market_outcomes["EV-LT3.5"] == ["le_3.4"]
    assert space.market_outcomes["EV-B3.5"] == ["gt_3.4_le_3.5", "gt_3.5_le_3.6"]


def quoted(event: Event, first: int) -> tuple[Event, list[str]]:
    """``event`` with a full quote on every market, and each quoted value as text.

    The prices sit on cent ticks, as the exchange's do, so a price that leaks after
    being rounded or snapped to cents still shows as one of the searched forms: the
    exact dollars, two-place dollars, whole or tenth cents with a ``c``, and bare
    cents. They lie in 0.61-0.78, numbers the briefs of these two events never
    contain otherwise. The sizes, volumes and open interest are odd enough (1739.25,
    48213, ...) that finding one can only mean it was copied. ``first`` keeps two
    events' values apart.
    """
    markets, values = [], []
    for i, m in enumerate(event.markets, start=first):
        bid = Decimal("0.61") + Decimal("0.01") * i
        prices = {
            "yes_bid": bid,
            "yes_ask": bid + Decimal("0.03"),
            "last_price": bid + Decimal("0.01"),
        }
        counts = {
            "yes_bid_size": Decimal("1739.25") + i,
            "yes_ask_size": Decimal("2861.75") + i,
            "volume": Decimal("48213") + i,
            "volume_24h": Decimal("7349") + i,
            "open_interest": Decimal("9187") + i,
        }
        priced = m.model_copy(update=prices | counts)
        assert priced.mid is not None
        markets.append(priced)
        for v in counts.values():
            values += [str(v), format(v.normalize(), "f")]
        for v in [*prices.values(), priced.mid]:
            cents = v * 100
            values += [str(v), format(v.normalize(), "f"), f"{v:.2f}"]
            values += [f"{cents:.0f}c", f"{cents:.1f}c", format(cents.normalize(), "f")]
    return event.model_copy(update={"markets": markets}), values


def test_event_brief_carries_shared_rules_and_no_prices():
    event, cpi_values = quoted(load_event("KXCPIYOY-26NOV"), first=0)
    space = build_outcome_space(event, series("KXCPIYOY"))
    brief = event_brief(event, space, series("KXCPIYOY"))
    assert brief.outcome_kind == "ladder" and brief.thresholds == [3.4, 3.5, 3.6, 3.7, 3.8]
    assert (
        "more than <the market's threshold> in the twelve months ending November 2026"
        in brief.rules
    )
    assert "3.6%" not in brief.rules
    text = brief.model_dump_json()
    assert [v for v in cpi_values if v in text] == []

    fed, fed_values = quoted(load_event("KXFEDDECISION-26DEC"), first=10)
    fed_space = build_outcome_space(fed, series("KXFEDDECISION"))
    fed_brief = event_brief(fed, fed_space, series("KXFEDDECISION"))
    assert [o.outcome_id for o in fed_brief.options] == fed_space.outcome_ids()
    assert fed_brief.rules.count("mutually exclusive") == 1
    assert fed_brief.closes_at == fed.close_time
    fed_text = fed_brief.model_dump_json()
    assert [v for v in fed_values if v in fed_text] == []


def test_realized_outcome_from_settled_markets():
    event = load_event("KXCPIYOY-26SEP")  # T3.5 yes, T3.6 yes, T3.7 no: reported 3.7
    space = build_outcome_space(event, series("KXCPIYOY"))
    results = {m.ticker: m.result for m in event.markets if m.result is not None}
    assert outcome_from_results(space, results) == "gt_3.6_le_3.7"
    assert ladder_outcome_for_value(space, 3.7) == "gt_3.6_le_3.7"
    assert ladder_outcome_for_value(space, 3.5) == "le_3.5"

    fed = build_outcome_space(load_event("KXFEDDECISION-26DEC"), series("KXFEDDECISION"))
    assert outcome_from_results(fed, {"KXFEDDECISION-26DEC-H0": Side.YES}) == "hold"
    assert outcome_from_results(fed, {"KXFEDDECISION-26DEC-H0": Side.NO}) is None
