"""Brier, skill, the abstain variant, reliability, and sample size on hand-computed cases."""

from __future__ import annotations

import pytest

from pmfa.evaluation.metrics import (
    brier_score,
    brier_skill,
    expected_calibration_error,
    market_brier_score,
    reliability,
    sample_size,
)

from .factories import frow

# Three rows, worked by hand:
#   p=0.8, price=0.6, YES: agent (0.8-1)^2 = 0.04, market (0.6-1)^2 = 0.16
#   p=0.3, price=0.5, NO:  agent (0.3-0)^2 = 0.09, market (0.5-0)^2 = 0.25
#   p=0.6, price=0.7, NO:  agent (0.6-0)^2 = 0.36, market (0.7-0)^2 = 0.49
# agent Brier = 0.49 / 3, market Brier = 0.90 / 3, skill = 1 - 0.49 / 0.90 = 41/90
ROWS = [
    frow(0.8, 0.6, True, event="A", market="A-1"),
    frow(0.3, 0.5, False, event="A", market="A-2"),
    frow(0.6, 0.7, False, event="B", market="B-1"),
]


def test_brier_score_and_market_brier_by_hand():
    assert brier_score(ROWS) == pytest.approx(0.49 / 3)
    assert market_brier_score(ROWS) == pytest.approx(0.90 / 3)


def test_brier_skill_by_hand():
    assert brier_skill(ROWS) == pytest.approx(41 / 90)


def test_skill_sign_conventions():
    # Better than the market: agent 0.01 vs market 0.16, skill = 1 - 0.01/0.16 = 0.9375 > 0.
    assert brier_skill([frow(0.9, 0.6, True)]) == pytest.approx(0.9375)
    # Worse: agent (0.2-1)^2 = 0.64 vs market 0.16, skill = 1 - 4 = -3 < 0.
    assert brier_skill([frow(0.2, 0.6, True)]) == pytest.approx(-3.0)
    # Forecasting the market price ties it: skill exactly 0.
    assert brier_skill([frow(0.6, 0.6, True), frow(0.3, 0.3, False)]) == 0.0


def test_skill_is_none_when_market_brier_is_zero():
    # The market priced 1.0 and YES happened: market Brier 0, skill undefined.
    assert brier_skill([frow(0.9, 1.0, True)]) is None


def test_empty_rows_have_no_scores():
    assert brier_score([]) is None
    assert brier_skill([]) is None
    assert expected_calibration_error([]) is None


# One confident correct forecast and one abstention on a coin-flip market that went NO:
#   row 1: p=0.9, price=0.5, YES: agent 0.01, market 0.25
#   row 2: abstained, price=0.5, NO: market 0.25
ABSTAIN = [frow(0.9, 0.5, True, market="A-1"), frow(None, 0.5, False, market="A-2")]


def test_abstentions_dropped_score_only_the_answered_rows():
    # Agent 0.01, market 0.25 on row 1 alone: skill = 1 - 0.01/0.25 = 0.96.
    assert brier_score(ABSTAIN) == pytest.approx(0.01)
    assert brier_skill(ABSTAIN) == pytest.approx(0.96)
    assert market_brier_score(ABSTAIN) == pytest.approx(0.25)


def test_abstain_as_market_scores_the_abstention_at_the_market_price():
    # Row 2 scored as p=0.5: agent (0.01 + 0.25)/2 = 0.13, market (0.25 + 0.25)/2 = 0.25,
    # skill = 1 - 0.26/0.50 = 0.48: abstaining no longer flatters the score.
    assert brier_score(ABSTAIN, abstain_as_market=True) == pytest.approx(0.13)
    assert brier_skill(ABSTAIN, abstain_as_market=True) == pytest.approx(0.48)
    assert brier_skill(ABSTAIN, abstain_as_market=True) < brier_skill(ABSTAIN)


def test_reliability_table_and_ece_by_hand():
    rows = [
        frow(0.2, 0.5, False),
        frow(0.4, 0.5, True),
        frow(0.5, 0.5, True),  # 0.5 is the lower edge of the second bin
        frow(0.8, 0.5, True),
        frow(1.0, 0.5, False),  # 1.0 falls in the last bin
    ]
    table = reliability(rows, bins=2)
    low, high = table.bins
    # Bin [0, 0.5): 0.2 (NO), 0.4 (YES): mean 0.3, observed 1/2, gap 0.2.
    assert (low.lower, low.upper, low.count) == (0.0, 0.5, 2)
    assert low.mean_forecast == pytest.approx(0.3)
    assert low.observed_frequency == pytest.approx(0.5)
    # Bin [0.5, 1]: 0.5, 0.8 (YES), 1.0 (NO): mean 2.3/3, observed 2/3, gap 0.3/3 = 0.1.
    assert high.count == 3
    assert high.mean_forecast == pytest.approx(2.3 / 3)
    assert high.observed_frequency == pytest.approx(2 / 3)
    # ECE = (2 x 0.2 + 3 x 0.1) / 5 = 0.7 / 5 = 0.14.
    assert table.ece == pytest.approx(0.14)
    assert expected_calibration_error(rows, bins=2) == pytest.approx(0.14)


def test_reliability_keeps_empty_bins_with_no_means():
    table = reliability([frow(0.05, 0.5, False)], bins=10)
    assert len(table.bins) == 10
    assert table.bins[0].count == 1
    assert all(b.count == 0 and b.mean_forecast is None for b in table.bins[1:])
    # One row: mean 0.05, observed 0, ECE 0.05.
    assert table.ece == pytest.approx(0.05)


def test_sample_size_reports_markets_and_events_side_by_side():
    rows = [
        frow(0.5, 0.5, True, event="A", market="A-1", horizon_days=7),
        frow(0.6, 0.5, True, event="A", market="A-1", horizon_days=1),
        frow(0.4, 0.5, False, event="A", market="A-2"),
        frow(None, 0.5, False, event="B", market="B-1"),
    ]
    size = sample_size(rows)
    assert (size.n_rows, size.n_markets, size.n_events, size.n_abstained) == (3, 2, 1, 1)
    size = sample_size(rows, abstain_as_market=True)
    assert (size.n_rows, size.n_markets, size.n_events) == (4, 3, 2)


def test_mixing_systems_is_refused():
    rows = [frow(0.5, 0.5, True), frow(0.5, 0.5, True, system="market")]
    with pytest.raises(ValueError, match="mix systems"):
        brier_score(rows)
