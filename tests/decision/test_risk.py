"""Risk state from an equity series: day start, peak, drawdown, and the sticky loss stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmfa.decision import risk_state, trading_day_start
from tests.decision.factories import CONFIG, PROPERTY

# 2026-11-25 is in US Eastern standard time: the trading day starts at 05:00 UTC.
DAY_START = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)


def at(hours: float) -> datetime:
    return DAY_START + timedelta(hours=hours)


def test_the_trading_day_follows_the_configured_time_zone():
    assert trading_day_start(at(3), "America/New_York") == DAY_START
    assert trading_day_start(DAY_START - timedelta(minutes=1), "America/New_York") == (
        DAY_START - timedelta(days=1)
    )
    summer = datetime(2026, 7, 1, 12, tzinfo=UTC)
    assert trading_day_start(summer, "America/New_York") == datetime(2026, 7, 1, 4, tzinfo=UTC)
    with pytest.raises(ValueError):
        trading_day_start(datetime(2026, 11, 25), "America/New_York")


def test_day_pnl_peak_and_drawdown():
    series = [(at(-30), D("1100")), (at(-2), D("1000")), (at(1), D("990")), (at(2), D("1012"))]
    state = risk_state(series, at(3), CONFIG)
    assert state.day_start_equity == D("1000")  # the last point before the day began
    assert state.peak_equity == D("1100")
    assert state.day_pnl == D("12")
    assert state.drawdown_fraction == (D("1100") - D("1012")) / D("1100")
    assert state.bankroll == D("1012")  # the current equity: what the dollar caps use
    assert not state.loss_stop_tripped and not state.drawdown_tripped


def test_a_tripped_loss_stop_stays_tripped_for_the_day():
    series = [(at(-1), D("1000")), (at(1), D("970")), (at(2), D("1050"))]
    state = risk_state(series, at(3), CONFIG)
    assert state.day_pnl == D("50")
    assert state.loss_stop_tripped  # -$30 is 3% of $1000, earlier today


def test_the_loss_stop_resets_on_the_next_trading_day():
    series = [(at(-1), D("1000")), (at(1), D("960")), (at(2), D("1000"))]
    tomorrow = at(25)
    assert not risk_state(series, tomorrow, CONFIG).loss_stop_tripped


def test_points_after_now_are_ignored():
    series = [(at(-1), D("1000")), (at(5), D("900"))]
    state = risk_state(series, at(1), CONFIG)
    assert state.day_pnl == 0 and not state.loss_stop_tripped


def test_the_drawdown_halt_trips_at_ten_percent():
    series = [(at(-48), D("1000")), (at(-1), D("900"))]
    state = risk_state(series, at(1), CONFIG)
    assert state.drawdown_fraction == D("0.1")
    assert state.drawdown_tripped
    assert not state.loss_stop_tripped  # the loss happened before today


def test_an_explicit_bankroll_is_the_caps_base_and_does_not_move_the_stop():
    # Day start 1000, down 29.50: under 3% of 1000 ($30), though over 3% of 970.50.
    series = [(at(-1), D("1000")), (at(1), D("970.50"))]
    state = risk_state(series, at(2), CONFIG, bankroll=D("970.50"))
    assert state.bankroll == D("970.50") and state.day_start_equity == D("1000")
    assert not state.loss_stop_tripped
    # A larger or smaller caps base leaves the stop where the day start put it.
    assert not risk_state(series, at(2), CONFIG, bankroll=D("500")).loss_stop_tripped
    down = [(at(-1), D("1000")), (at(1), D("970"))]
    assert risk_state(down, at(2), CONFIG, bankroll=D("5000")).loss_stop_tripped


def test_the_first_point_of_the_day_is_the_start_when_nothing_precedes_it():
    state = risk_state([(at(1), D("1000")), (at(2), D("969"))], at(3), CONFIG)
    assert state.day_start_equity == D("1000") and state.loss_stop_tripped


def test_an_empty_series_needs_a_bankroll():
    state = risk_state([], at(1), CONFIG, bankroll=D("1000"))
    assert state.day_start_equity == D("1000") and state.day_pnl == 0
    with pytest.raises(ValueError):
        risk_state([], at(1), CONFIG)


@PROPERTY
@given(
    start=st.integers(min_value=100, max_value=100000).map(D),
    moves=st.lists(st.integers(min_value=-500, max_value=500), min_size=1, max_size=12),
    later=st.integers(min_value=0, max_value=600),
)
def test_once_tripped_the_loss_stop_stays_tripped_until_the_day_ends(start, moves, later):
    # Equity moves in basis points of the start, once per minute from the day's start.
    series = [(at(-1), start)]
    for i, bp in enumerate(moves):
        series.append((DAY_START + timedelta(minutes=i + 1), start * (1 + D(bp) / 10000)))
    stop = CONFIG.caps.daily_loss_stop_fraction * start
    first_trip = next(
        (t for t, e in series[1:] if e - start <= -stop),
        None,
    )
    end_of_day = DAY_START + timedelta(days=1)
    for t, _ in series[1:]:
        state = risk_state(series, t, CONFIG)
        assert state.loss_stop_tripped == (first_trip is not None and t >= first_trip)
    if first_trip is not None:
        # Equity recovers above the start, and the stop still holds for the rest of the day.
        recovered = [*series, (series[-1][0] + timedelta(minutes=1), start * 2)]
        now = min(end_of_day - timedelta(seconds=1), recovered[-1][0] + timedelta(minutes=later))
        assert risk_state(recovered, now, CONFIG).loss_stop_tripped
        assert not risk_state(recovered, end_of_day + timedelta(hours=1), CONFIG).loss_stop_tripped
