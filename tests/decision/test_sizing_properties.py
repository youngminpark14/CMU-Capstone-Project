"""Seam 2 properties of sizing, checked against arithmetic written out again here."""

from __future__ import annotations

import math
from decimal import Decimal as D

from hypothesis import given
from hypothesis import strategies as st

from pmfa.decision import SizingInputs, fee, gate, is_valid_tick, size_event
from pmfa.records.market import BookLevel, Market, OrderBook, Position, Side
from tests.decision.factories import (
    CONFIG,
    EVENT,
    NOW,
    PROPERTY,
    gate_context,
    market,
    series,
    series_config,
)

CAPS = CONFIG.caps
MIN_EDGE = CONFIG.sizing.min_edge
TICKERS = [f"{EVENT}-M{i}" for i in range(3)]


def _cents(lo: int, hi: int) -> st.SearchStrategy[D]:
    return st.integers(min_value=lo, max_value=hi).map(lambda n: D(n) / 100)


@st.composite
def books(draw: st.DrawFn, ticker: str) -> OrderBook:
    """An uncrossed book: best YES bid below the YES ask (1 - best NO bid)."""
    yes_bid = draw(st.integers(min_value=1, max_value=97))
    spread = draw(st.integers(min_value=1, max_value=98 - yes_bid))
    no_bid = 100 - yes_bid - spread
    counts = st.integers(min_value=1, max_value=20000).map(lambda n: D(n) / 100)

    def levels(best: int) -> list[BookLevel]:
        prices = sorted(
            draw(st.sets(st.integers(min_value=1, max_value=best), min_size=0, max_size=3))
            | {best},
            reverse=True,
        )
        return [BookLevel(price=D(p) / 100, contracts=draw(counts)) for p in prices]

    return OrderBook(ticker=ticker, as_of=NOW, yes_bids=levels(yes_bid), no_bids=levels(no_bid))


@st.composite
def sizing_inputs(draw: st.DrawFn) -> SizingInputs:
    tickers = TICKERS[: draw(st.integers(min_value=1, max_value=3))]
    book_map = {t: draw(books(t)) for t in tickers}
    liquidity = {
        t: {
            s: draw(st.sampled_from([D("0"), D("499.99"), D("500"), *[D("5000")] * 6]))
            for s in Side
        }
        for t in tickers
    }
    positions = []
    for t in tickers:
        if draw(st.integers(min_value=0, max_value=2)) == 0:
            positions.append(
                Position(
                    market_ticker=t,
                    event_ticker=EVENT,
                    side=draw(st.sampled_from(Side)),
                    contracts=draw(st.integers(min_value=1, max_value=120)),
                    avg_price=draw(_cents(1, 99)),
                )
            )
    return SizingInputs(
        episode_id="ep-prop",
        event_ticker=EVENT,
        forecast_yes={t: draw(st.floats(min_value=0, max_value=1)) for t in tickers},
        markets=[market(t) for t in tickers],
        books=book_map,
        liquidity=liquidity,
        positions=positions,
        bankroll=D(draw(st.integers(min_value=10, max_value=200000))),
        confidence=draw(st.floats(min_value=0, max_value=1)),
        series=series(
            fee_type=draw(st.sampled_from(["quadratic", "quadratic_with_maker_fees"])),
            multiplier=draw(st.sampled_from(["0.5", "1", "2"])),
        ),
        series_config=series_config(draw(st.sampled_from([0.0, 0.25, 0.5, 1.0]))),
        created_at=NOW,
    )


# Independent arithmetic for the checks below.


def _p_side(inputs: SizingInputs, ticker: str, side: Side) -> D:
    book = inputs.books[ticker]
    mid = (book.yes_bids[0].price + (1 - book.no_bids[0].price)) / 2
    k = D(str(inputs.series_config.shrinkage))
    p_yes = k * mid + (1 - k) * D(str(inputs.forecast_yes[ticker]))
    return p_yes if side is Side.YES else 1 - p_yes


def _edge(inputs: SizingInputs, ticker: str, side: Side, price: D) -> D:
    rate = D("0.07") * inputs.series.fee_multiplier
    return _p_side(inputs, ticker, side) - price - rate * price * (1 - price)


def _best_ask(inputs: SizingInputs, ticker: str, side: Side) -> D:
    other = inputs.books[ticker].no_bids if side is Side.YES else inputs.books[ticker].yes_bids
    return 1 - other[0].price


def _best_eligible_edge(inputs: SizingInputs) -> D | None:
    edges = [
        _edge(inputs, t, s, _best_ask(inputs, t, s))
        for t in inputs.books
        for s in Side
        if inputs.liquidity[t][s] >= CAPS.liquidity_floor_dollars
    ]
    return max(edges) if edges else None


def _exposures(inputs: SizingInputs, ticker: str, side: Side, n: int, limit: D, fee_: D):
    """Dollars at cost in the market and in the event after the order."""
    held = {p.market_ticker: p for p in inputs.positions}
    others = sum((p.avg_price * p.contracts for t, p in held.items() if t != ticker), D(0))
    pos = held.get(ticker)
    if pos is None:
        here = n * limit + fee_
    elif pos.side is side:
        here = pos.avg_price * pos.contracts + n * limit + fee_
    elif n <= pos.contracts:
        here = pos.avg_price * (pos.contracts - n) + fee_
    else:
        here = (n - pos.contracts) * limit + fee_
    return here, others + here


@PROPERTY
@given(sizing_inputs())
def test_size_never_exceeds_any_cap(inputs):
    p = size_event(inputs, CONFIG).proposal
    if p is None:
        return
    assert 0 < p.contracts <= CAPS.max_contracts_per_order
    in_market, in_event = _exposures(
        inputs, p.market_ticker, p.side, p.contracts, p.limit_price, p.fee
    )
    assert in_market <= CAPS.per_market_fraction * inputs.bankroll
    assert in_event <= CAPS.per_event_fraction * inputs.bankroll
    asks = inputs.books[p.market_ticker].asks(p.side)
    depth = sum((lvl.contracts for lvl in asks if lvl.price <= p.edge_bound_price), D(0))
    assert p.contracts <= math.floor(depth)
    assert p.limit_price <= p.edge_bound_price
    assert is_valid_tick(p.limit_price, market(p.market_ticker), p.side)
    assert inputs.liquidity[p.market_ticker][p.side] >= CAPS.liquidity_floor_dollars


@PROPERTY
@given(sizing_inputs())
def test_size_is_zero_whenever_edge_after_fees_is_below_the_minimum(inputs):
    p = size_event(inputs, CONFIG).proposal
    best = _best_eligible_edge(inputs)
    if best is None or best < MIN_EDGE:
        assert p is None
    if p is not None:
        rounded = fee(
            p.contracts, p.limit_price, inputs.series.fee_type, inputs.series.fee_multiplier
        )
        assert p.fee == rounded
        recomputed = (
            _p_side(inputs, p.market_ticker, p.side) - p.limit_price - rounded / p.contracts
        )
        assert p.edge == recomputed
        assert p.edge >= MIN_EDGE
        # The bound is the last tick keeping the edge: the next cent up does not.
        assert _edge(inputs, p.market_ticker, p.side, p.edge_bound_price) >= MIN_EDGE
        nxt = p.edge_bound_price + D("0.01")
        assert nxt >= 1 or _edge(inputs, p.market_ticker, p.side, nxt) < MIN_EDGE


@PROPERTY
@given(sizing_inputs(), st.floats(min_value=0, max_value=1))
def test_size_does_not_grow_when_confidence_falls(inputs, factor):
    high = size_event(inputs, CONFIG).proposal
    low = size_event(inputs.model_copy(update={"confidence": inputs.confidence * factor}), CONFIG)
    if low.proposal is None:
        return
    assert high is not None
    assert (low.proposal.market_ticker, low.proposal.side) == (high.market_ticker, high.side)
    assert low.proposal.contracts <= high.contracts


@PROPERTY
@given(sizing_inputs())
def test_direction_always_matches_the_sign_of_the_edge(inputs):
    p = size_event(inputs, CONFIG).proposal
    if p is None:
        return
    t = p.market_ticker
    assert _edge(inputs, t, p.side, _best_ask(inputs, t, p.side)) > 0
    assert _edge(inputs, t, p.side.opposite, _best_ask(inputs, t, p.side.opposite)) < 0
    delta = p.position_target - p.position_before
    assert (delta > 0) == (p.side is Side.YES) and delta != 0
    # Probability and price are both on the chosen side's scale.
    assert p.shrunk_probability > float(p.limit_price)
    yes_probability = p.shrunk_probability if p.side is Side.YES else 1 - p.shrunk_probability
    yes_mid = float(p.market_mid)
    assert (yes_probability > yes_mid) == (p.side is Side.YES)


@PROPERTY
@given(sizing_inputs())
def test_the_gate_independently_approves_what_sizing_proposes(inputs):
    p = size_event(inputs, CONFIG).proposal
    if p is None:
        return
    context = gate_context(
        inputs,
        market_ticker=p.market_ticker,
        liquidity_dollars=inputs.liquidity[p.market_ticker][p.side],
    )
    result = gate(p, context)
    assert result.approved, result.reasons


def test_a_market_needs_a_book_and_a_forecast():
    m: Market = market(TICKERS[0])
    lone = SizingInputs(
        episode_id="ep",
        event_ticker=EVENT,
        forecast_yes={},
        markets=[m],
        books={},
        bankroll=D("1000"),
        confidence=1.0,
        series=series(),
        series_config=series_config(),
        created_at=NOW,
    )
    assert size_event(lone, CONFIG).proposal is None
