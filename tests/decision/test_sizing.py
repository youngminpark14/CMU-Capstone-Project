"""Sizing on hand-worked cases: Kelly, caps, depth, the edge bound, netting, rounding."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from pmfa.config import Config, FollowConfig
from pmfa.decision import UnknownFeeType, edge_bound, shrink, size_event, size_fn, summarize
from pmfa.decision.sizing import AT_TARGET, BLOCKED, NO_EDGE, NO_LIQUIDITY, NO_ROOM
from pmfa.records.decision import SizingSummary
from pmfa.records.forecast import Forecast
from pmfa.records.market import Side
from tests.decision.factories import (
    CONFIG,
    CUT,
    EVENT,
    HOLD,
    NOW,
    book,
    inputs,
    market,
    position,
    series,
)

# The default book: YES bid 0.48, NO bid 0.50, so YES asks at 0.50 and NO asks at 0.52.


def test_quarter_kelly_is_clipped_to_the_per_order_cap():
    # p = 0.70, c = 0.50 + 0.0175 = 0.5175: f* = 0.3782; x 0.25 x $1000 / c = 182 contracts.
    decision = size_event(inputs(), CONFIG)
    p = decision.proposal
    assert p is not None
    assert (p.market_ticker, p.side, p.contracts) == (HOLD, Side.YES, 25)
    assert p.limit_price == D("0.50")
    assert p.fee == D("0.44")  # 0.07 x 25 x 0.25 = 0.4375, rounded up
    assert p.cost == D("12.94")
    assert p.edge == D("0.70") - D("0.50") - D("0.44") / 25
    assert p.reference_price == D("0.50")
    assert p.market_mid == D("0.49")
    assert (p.position_before, p.position_target) == (0, 25)
    assert p.edge_bound_price == D("0.66")
    assert p.expires_at == NOW.replace(minute=10)
    assert p.order_type == "limit" and p.time_in_force == "immediate_or_cancel"


def test_confidence_scales_the_kelly_size():
    # 182.7 contracts at full confidence; 0.1 of that floors to 18.
    p = size_event(inputs(confidence=0.1), CONFIG).proposal
    assert p is not None and p.contracts == 18


def test_shrinkage_pulls_the_forecast_to_the_mid():
    assert shrink(0.70, D("0.49"), 0.5) == D("0.595")
    p = size_event(inputs(shrinkage=0.5), CONFIG).proposal
    assert p is not None
    assert p.shrunk_probability == pytest.approx(0.595)
    assert p.forecast_probability == pytest.approx(0.70)


def test_buying_no_when_the_forecast_is_below_the_market():
    p = size_event(inputs(forecast={HOLD: 0.20}), CONFIG).proposal
    assert p is not None
    assert p.side is Side.NO
    assert p.limit_price == D("0.52")  # 1 - YES bid 0.48
    assert p.forecast_probability == pytest.approx(0.80)
    assert p.position_target == -p.contracts


def test_no_edge_when_the_market_already_prices_the_forecast():
    decision = size_event(inputs(forecast={HOLD: 0.51}), CONFIG)
    assert decision.proposal is None
    assert decision.reason == NO_EDGE
    assert decision.best is not None and decision.best.edge < D("0.02")


def test_the_liquidity_floor_excludes_an_otherwise_better_market():
    books = {HOLD: book(HOLD), CUT: book(CUT)}
    liquidity = {
        HOLD: {Side.YES: D("1000"), Side.NO: D("1000")},
        CUT: {Side.YES: D("499.99"), Side.NO: D("1000")},
    }
    decision = size_event(
        inputs(forecast={HOLD: 0.60, CUT: 0.90}, books=books, liquidity=liquidity), CONFIG
    )
    assert decision.proposal is not None
    assert decision.proposal.market_ticker == HOLD

    nothing = size_event(inputs(liquidity={HOLD: {}}), CONFIG)
    assert nothing.proposal is None and nothing.reason == NO_LIQUIDITY


def test_the_best_edge_wins_across_markets():
    books = {HOLD: book(HOLD), CUT: book(CUT)}
    p = size_event(inputs(forecast={HOLD: 0.60, CUT: 0.90}, books=books), CONFIG).proposal
    assert p is not None and p.market_ticker == CUT


def test_a_blocklisted_market_does_not_take_the_proposal_from_the_others():
    books = {HOLD: book(HOLD), CUT: book(CUT)}
    sized = inputs(forecast={HOLD: 0.60, CUT: 0.90}, books=books)
    p = size_event(sized, Config(follow=FollowConfig(blocklist=[CUT]))).proposal
    assert p is not None and p.market_ticker == HOLD

    for blocked in ([HOLD, CUT], [EVENT], ["KXFEDDECISION"]):
        decision = size_event(sized, Config(follow=FollowConfig(blocklist=blocked)))
        assert decision.proposal is None and decision.reason == BLOCKED


def test_the_edge_bound_is_the_last_tick_that_keeps_the_minimum_edge():
    p_side = D("0.60")
    bound = edge_bound(p_side, market(), Side.YES, "quadratic", D("1"), D("0.02"))
    assert bound == D("0.56")

    def edge(t: D) -> D:
        return p_side - t - D("0.07") * t * (1 - t)

    assert edge(D("0.56")) >= D("0.02") > edge(D("0.57"))


def test_depth_beyond_the_edge_bound_is_not_used():
    # YES asks: 0.50 x 10, 0.52 x 10, 0.60 x 100. With p = 0.60 the bound is 0.56.
    deep = book(no_bids=[("0.50", "10"), ("0.48", "10"), ("0.40", "100")])
    p = size_event(inputs(forecast={HOLD: 0.60}, books={HOLD: deep}), CONFIG).proposal
    assert p is not None
    assert p.contracts == 20
    assert p.limit_price == D("0.52")  # the highest level actually needed
    assert p.edge_bound_price == D("0.56")


def test_fractional_levels_offer_only_their_whole_contracts():
    # YES asks 0.50 x 10.5 and 0.51 x 10.5: each level fills 10 whole contracts, so 20
    # in all, not floor(21.0) = 21; the limit is the second level.
    frac = book(no_bids=[("0.50", "10.5"), ("0.49", "10.5")])
    p = size_event(inputs(forecast={HOLD: 0.80}, books={HOLD: frac}), CONFIG).proposal
    assert p is not None
    assert (p.contracts, p.limit_price) == (20, D("0.51"))

    # A level of 10.5 alone covers 10 contracts at its own price, not 11.
    p = size_event(
        inputs(forecast={HOLD: 0.80}, books={HOLD: book(no_bids=[("0.50", "10.5")])}), CONFIG
    ).proposal
    assert p is not None and (p.contracts, p.limit_price) == (10, D("0.50"))


def test_fractional_levels_that_add_up_to_a_contract_do_not_set_the_limit():
    # YES asks 0.49 x 0.5, 0.50 x 0.5, 0.51 x 10, and a confidence small enough for one
    # contract. The two halves add up to 1.0, but neither holds a whole contract, so the
    # walk must reach 0.51: a limit of 0.50 would fill nothing.
    frac = book(no_bids=[("0.51", "0.5"), ("0.50", "0.5"), ("0.49", "10")])
    p = size_event(
        inputs(forecast={HOLD: 0.80}, books={HOLD: frac}, confidence=0.005), CONFIG
    ).proposal
    assert p is not None
    assert (p.contracts, p.limit_price) == (1, D("0.51"))


def test_the_limit_is_the_highest_level_needed_to_fill():
    deep = book(no_bids=[("0.50", "10"), ("0.48", "10"), ("0.40", "100")])
    p = size_event(inputs(forecast={HOLD: 0.70}, books={HOLD: deep}), CONFIG).proposal
    assert p is not None
    assert p.contracts == 25
    assert p.limit_price == D("0.60")
    assert p.fee == D("0.42")  # 0.07 x 25 x 0.6 x 0.4


def test_buying_the_other_side_nets_against_a_holding():
    held = position(side=Side.NO, contracts=10, avg="0.40")
    p = size_event(inputs(positions=[held]), CONFIG).proposal
    assert p is not None
    assert p.side is Side.YES
    assert p.position_before == -10
    assert p.position_target == p.position_before + p.contracts
    assert p.contracts == 25  # 10 to close the NO holding, then YES, clipped to the order cap


def test_an_opposite_holding_is_covered_when_the_target_floors_to_zero():
    # YES has the best edge; at confidence 0.001 Kelly floors to 0 contracts, so the
    # order covers the 10 NO held and nothing more.
    held = position(side=Side.NO, contracts=10, avg="0.50")
    decision = size_event(inputs(confidence=0.001, positions=[held]), CONFIG)
    p = decision.proposal
    assert p is not None, decision.reason
    assert (p.side, p.contracts) == (Side.YES, 10)
    assert (p.position_before, p.position_target) == (-10, 0)


def test_a_holding_whose_side_still_has_edge_is_not_trimmed():
    # Kelly wants 18 YES at confidence 0.1; 30 are held: no trade, holdings left alone.
    held = position(side=Side.YES, contracts=30)
    decision = size_event(inputs(confidence=0.1, positions=[held]), CONFIG)
    assert decision.proposal is None and decision.reason == AT_TARGET

    smaller = position(side=Side.YES, contracts=10)
    p = size_event(inputs(confidence=0.1, positions=[smaller]), CONFIG).proposal
    assert p is not None and p.contracts == 8 and p.position_target == 18


def test_the_event_cap_counts_holdings_in_other_markets():
    # $100 event cap; $95 already in another market leaves room for 9 contracts at 0.50.
    other = position(ticker=CUT, side=Side.YES, contracts=190, avg="0.50")
    p = size_event(inputs(positions=[other]), CONFIG).proposal
    assert p is not None
    assert p.contracts * p.limit_price + p.fee <= D("5")
    assert p.contracts == 9


def test_the_market_cap_counts_the_holding_in_the_market():
    # $50 market cap; 90 YES held at 0.50 = $45 leaves room for 9 more.
    held = position(side=Side.YES, contracts=90, avg="0.50")
    p = size_event(inputs(positions=[held]), CONFIG).proposal
    assert p is not None
    assert D("45") + p.cost <= D("50")
    assert p.contracts == 9


def _rounding_case(bankroll: str):
    # p = 0.5385 at an ask of 0.50: the edge is 0.021 with the unrounded fee (0.0175).
    # One contract rounds the fee to 0.02 (edge 0.0185); four round to 0.07 (edge 0.021).
    # Shrinkage 0.5 toward the 0.495 mid: forecast 0.582 gives 0.5385.
    tight = book(yes_bids=[("0.49", "100")], no_bids=[("0.50", "100")])
    return inputs(forecast={HOLD: 0.582}, books={HOLD: tight}, bankroll=bankroll, shrinkage=0.5)


def test_fee_rounding_that_breaks_the_minimum_edge_means_no_trade():
    decision = size_event(_rounding_case("50"), CONFIG)  # Kelly target: 1 contract
    assert decision.proposal is None
    assert decision.reason == NO_ROOM


def test_the_reported_edge_holds_after_rounding():
    p = size_event(_rounding_case("200"), CONFIG).proposal  # Kelly target: 4 contracts
    assert p is not None
    assert p.contracts == 4 and p.fee == D("0.07")
    assert p.edge >= D("0.02")


def test_an_event_without_books_or_forecasts_has_no_candidates():
    decision = size_event(inputs(forecast={}), CONFIG)
    assert decision.proposal is None and decision.candidates == []


def test_unknown_fee_types_are_refused():
    with pytest.raises(UnknownFeeType):
        size_event(inputs(fee_series=series(fee_type="flat")), CONFIG)


def test_summarize_reports_what_would_trade():
    decision = size_event(inputs(), CONFIG)
    summary = summarize(decision)
    assert summary == SizingSummary(
        market_ticker=HOLD, side=Side.YES, contracts=25, cost=D("12.94")
    )
    assert summary.trades
    assert summarize(size_event(inputs(forecast={HOLD: 0.5}), CONFIG)) == SizingSummary()


def test_positions_in_other_events_are_ignored():
    elsewhere = position(ticker="KXCPIYOY-26NOV-T3.0", contracts=1000).model_copy(
        update={"event_ticker": "KXCPIYOY-26NOV"}
    )
    p = size_event(inputs(positions=[elsewhere]), CONFIG).proposal
    assert p is not None and p.contracts == 25 and p.event_ticker == EVENT


def test_size_fn_sizes_a_forecast_for_the_search():
    forecast = Forecast(
        forecast_id="f-1",
        episode_id="ep-1",
        event_ticker=EVENT,
        as_of=NOW,
        outcome_probabilities={"hold": 0.7, "cut": 0.3},
        market_probabilities={HOLD: 0.70},
        coverage=0.9,
        confidence=0.1,
        n_independent_origins=3,
    )
    summary = size_fn(inputs(forecast={HOLD: 0.5}), CONFIG)(forecast)
    assert summary.market_ticker == HOLD and summary.side is Side.YES
    assert summary.contracts == 18  # the forecast's probability and confidence were used
