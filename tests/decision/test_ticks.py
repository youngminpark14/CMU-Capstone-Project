"""Ticks: snapping to a market's valid prices on either side."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmfa.decision import is_valid_tick, snap_down, snap_up, valid_ticks
from pmfa.records.market import PriceRange, Side
from tests.decision.factories import PROPERTY, market

TAPERED = [
    PriceRange(start="0", end="0.10", step=D("0.001")),
    PriceRange(start="0.10", end="0.90", step=D("0.01")),
    PriceRange(start="0.90", end="1", step=D("0.001")),
]


def test_a_market_without_ranges_uses_the_one_cent_grid():
    ticks = valid_ticks(market())
    assert ticks[0] == D("0.01") and ticks[-1] == D("0.99") and len(ticks) == 99


@pytest.mark.parametrize(
    ("price", "down", "up"),
    [
        (D("0.505"), D("0.50"), D("0.51")),
        (D("0.50"), D("0.50"), D("0.50")),
        (D("0.0555"), D("0.055"), D("0.056")),
        (D("0.0005"), None, D("0.001")),
        (D("0.9995"), D("0.999"), None),
    ],
)
def test_snap_down_and_up_on_tapered_ranges(price, down, up):
    m = market(price_ranges=TAPERED)
    assert snap_down(price, m) == down
    assert snap_up(price, m) == up


def test_zero_and_one_are_never_valid():
    m = market(price_ranges=TAPERED)
    assert not is_valid_tick(D("0"), m)
    assert not is_valid_tick(D("1"), m)
    assert is_valid_tick(D("0.001"), m)
    assert not is_valid_tick(D("0.105"), m)


def test_no_prices_mirror_the_yes_grid():
    ranges = [PriceRange(start="0.01", end="0.99", step=D("0.02"))]  # 0.01, 0.03, ..., 0.99
    m = market(price_ranges=ranges)
    assert is_valid_tick(D("0.49"), m, Side.YES)
    assert not is_valid_tick(D("0.50"), m, Side.YES)
    assert is_valid_tick(D("0.51"), m, Side.NO)  # 1 - 0.51 = 0.49 is a YES tick
    assert snap_down(D("0.50"), m, Side.NO) == D("0.49")
    assert snap_up(D("0.50"), m, Side.NO) == D("0.51")


@PROPERTY
@given(st.integers(min_value=0, max_value=10000).map(lambda n: D(n) / 10000), st.sampled_from(Side))
def test_snapped_prices_are_valid_and_bracket_the_price(price, side):
    m = market(price_ranges=TAPERED)
    down, up = snap_down(price, m, side), snap_up(price, m, side)
    if down is not None:
        assert down <= price and is_valid_tick(down, m, side)
    if up is not None:
        assert up >= price and is_valid_tick(up, m, side)
    assert is_valid_tick(price, m, side) == (down == price)
