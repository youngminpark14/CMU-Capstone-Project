"""Confidence and the origin rule."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmfa.decision import confidence, origin_rule_met
from tests.decision.factories import CONFIG, PRIMARY, PROPERTY, secondary


@pytest.mark.parametrize(
    ("coverage", "origins", "exhausted", "expected"),
    [
        (0.9, 3, False, 0.9),
        (0.9, 6, False, 0.9),
        (0.9, 1, False, 0.3),
        (0.8, 2, True, 0.8 * 2 / 3 / 2),
        (0.7, 0, False, 0.0),
    ],
)
def test_confidence_formula(coverage, origins, exhausted, expected):
    assert confidence(coverage, origins, exhausted, CONFIG) == pytest.approx(expected)


@PROPERTY
@given(
    coverage=st.floats(min_value=0, max_value=1),
    origins=st.integers(min_value=0, max_value=10),
    exhausted=st.booleans(),
)
def test_confidence_can_only_reduce_size(coverage, origins, exhausted):
    value = confidence(coverage, origins, exhausted, CONFIG)
    assert 0 <= value <= coverage <= 1
    assert confidence(coverage, origins, True, CONFIG) <= value or exhausted


def test_one_primary_origin_is_enough():
    assert origin_rule_met([PRIMARY])


def test_secondary_origins_need_two_independent_ones():
    assert not origin_rule_met([])
    assert not origin_rule_met([secondary(1)])
    assert not origin_rule_met([secondary(1), secondary(1)])  # the same origin twice
    assert origin_rule_met([secondary(1), secondary(2)])
