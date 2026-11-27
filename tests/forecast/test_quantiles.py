"""From five quantiles to ladder probabilities: interpolation, the tail rule, the half-step shift."""

from __future__ import annotations

import itertools
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pmfa.forecast import QuantileCdf, bin_probabilities, build_outcome_space, market_probabilities
from pmfa.records.roles import Quantiles

from .scripted import load_event, series

Q = Quantiles(q05=3.2, q25=3.4, q50=3.5, q75=3.6, q95=3.9)


def _cpi_space(name: str = "KXCPIYOY-26NOV"):
    event = load_event(name)
    return build_outcome_space(event, series("KXCPIYOY"))


def test_cdf_passes_through_the_quantiles_and_is_linear_between_them():
    cdf = QuantileCdf(Q, fallback_width=0.1)
    for x, p in [(3.2, 0.05), (3.4, 0.25), (3.5, 0.5), (3.6, 0.75), (3.9, 0.95)]:
        assert cdf(x) == pytest.approx(p)
    assert cdf(3.45) == pytest.approx(0.375)


def test_tail_rule_matches_the_adjacent_segment_density_and_carries_five_percent():
    cdf = QuantileCdf(Q, fallback_width=0.1)
    h = 1e-6
    upper_segment = (0.95 - 0.75) / (3.9 - 3.6)
    lower_segment = (0.25 - 0.05) / (3.4 - 3.2)
    assert (cdf(3.9 + h) - cdf(3.9)) / h == pytest.approx(upper_segment, rel=1e-3)
    assert (cdf(3.2) - cdf(3.2 - h)) / h == pytest.approx(lower_segment, rel=1e-3)
    assert 1 - cdf(3.9) == pytest.approx(0.05)
    assert cdf(4.5) < 1.0 and cdf(2.5) > 0.0
    # exponential: the survival halves every ln(2)/rate beyond q95
    rate = upper_segment / 0.05
    assert (1 - cdf(3.9 + math.log(2) / rate)) == pytest.approx(0.025)


def test_equal_neighbouring_quantiles_are_a_point_mass_without_dividing_by_zero():
    tied = Quantiles(q05=3.5, q25=3.5, q50=3.5, q75=3.6, q95=3.6)
    cdf = QuantileCdf(tied, fallback_width=0.1)
    assert cdf(3.5) == pytest.approx(0.5)  # jump from 0.05 to 0.5 at 3.5, right-continuous
    assert cdf(3.5 - 1e-9) == pytest.approx(0.05, abs=1e-6)
    assert cdf(3.6) == pytest.approx(0.95)
    # tails take the density of the nearest segment with positive width: (0.75-0.5)/0.1
    assert cdf.lower_density == pytest.approx(2.5) and cdf.upper_density == pytest.approx(2.5)

    flat = Quantiles(q05=3.6, q25=3.6, q50=3.6, q75=3.6, q95=3.6)
    cdf = QuantileCdf(flat, fallback_width=0.1)
    assert cdf(3.6) == pytest.approx(0.95) and cdf(3.6 - 1e-9) == pytest.approx(0.05, abs=1e-6)
    assert math.isfinite(cdf.upper_rate) and cdf.upper_rate > 0


def test_reported_steps_shift_thresholds_by_half_an_increment():
    space = _cpi_space()
    probs = market_probabilities(space, bin_probabilities(space, Q))
    cdf = QuantileCdf(Q, fallback_width=0.1)
    # "above 3.6" on a number reported to one decimal: the unrounded value is at least 3.65
    assert probs["KXCPIYOY-26NOV-T3.6"] == pytest.approx(1 - cdf(3.65), rel=1e-6)
    assert probs["KXCPIYOY-26NOV-T3.4"] == pytest.approx(1 - cdf(3.45), rel=1e-6)

    flat = Quantiles(q05=3.6, q25=3.6, q50=3.6, q75=3.6, q95=3.6)
    flat_probs = market_probabilities(space, bin_probabilities(space, flat))
    assert flat_probs["KXCPIYOY-26NOV-T3.5"] > 0.95  # a reported 3.6 is above 3.5 ...
    assert flat_probs["KXCPIYOY-26NOV-T3.6"] < 0.05  # ... but not above 3.6


def test_ladder_probabilities_are_monotone_and_strictly_inside_zero_and_one():
    space = _cpi_space()
    bins = bin_probabilities(space, Q)
    assert sum(bins.values()) == pytest.approx(1.0)
    probs = market_probabilities(space, bins)
    ordered = [probs[f"KXCPIYOY-26NOV-T{s}"] for s in ("3.4", "3.5", "3.6", "3.7", "3.8")]
    assert ordered == sorted(ordered, reverse=True)
    assert all(0.0 < p < 1.0 for p in ordered)


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.floats(min_value=-5, max_value=15, allow_nan=False, allow_infinity=False),
        min_size=5,
        max_size=5,
    )
)
def test_any_quantiles_give_monotone_probabilities_never_exactly_zero_or_one(values):
    values = sorted(values)
    q = Quantiles(q05=values[0], q25=values[1], q50=values[2], q75=values[3], q95=values[4])
    space = _cpi_space()
    probs = market_probabilities(space, bin_probabilities(space, q))
    ordered = [probs[f"KXCPIYOY-26NOV-T{s}"] for s in ("3.4", "3.5", "3.6", "3.7", "3.8")]
    assert all(a >= b for a, b in itertools.pairwise(ordered))
    assert all(0.0 < p < 1.0 for p in ordered)
