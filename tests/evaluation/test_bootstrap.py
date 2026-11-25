"""The clustered bootstrap: seeded, event-level, and honest about a single cluster."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmfa.evaluation.bootstrap import brier_interval, pnl_interval, skill_interval
from pmfa.evaluation.metrics import brier_score, brier_skill
from pmfa.evaluation.rows import ForecastRow
from pmfa.records.market import Side

from .factories import frow, trade


def correlated_rows() -> list[ForecastRow]:
    """Ten events of five markets each; the markets of an event resolve together.

    The forecast is 0.7 everywhere, so a row's squared error is 0.09 when its
    event went YES and 0.49 when it went NO, the same for all five markets.
    """
    rows = []
    for e in range(10):
        outcome = e % 3 != 0
        price = 0.6 if outcome else 0.5
        for m in range(5):
            rows.append(frow(0.7, price, outcome, event=f"E{e}", market=f"E{e}-{m}"))
    return rows


def as_independent_markets(rows: list[ForecastRow]) -> list[ForecastRow]:
    """The same rows with each market as its own cluster: the naive per-market bootstrap."""
    return [r.model_copy(update={"event_ticker": r.market_ticker}) for r in rows]


def test_point_estimates_match_the_metrics():
    rows = correlated_rows()
    assert brier_interval(rows, seed=1).estimate == pytest.approx(brier_score(rows))
    assert skill_interval(rows, seed=1).estimate == pytest.approx(brier_skill(rows))


def test_counts_clusters_and_rows():
    interval = brier_interval(correlated_rows(), seed=1)
    assert (interval.n_clusters, interval.n_rows) == (10, 50)


def test_same_seed_gives_the_same_interval():
    rows = correlated_rows()
    first = brier_interval(rows, seed=7, samples=500)
    assert brier_interval(rows, seed=7, samples=500) == first
    assert skill_interval(rows, seed=7, samples=500) == skill_interval(rows, seed=7, samples=500)
    assert brier_interval(rows, seed=8, samples=500) != first


def test_resampling_events_is_wider_than_resampling_markets_when_markets_move_together():
    rows = correlated_rows()
    clustered = brier_interval(rows, seed=3)
    naive = brier_interval(as_independent_markets(rows), seed=3)
    assert naive.n_clusters == 50
    clustered_width = clustered.upper - clustered.lower
    naive_width = naive.upper - naive.lower
    # Five identical markets per event: the honest interval is about sqrt(5) times wider.
    assert clustered_width > 1.5 * naive_width


def test_two_clusters_by_hand():
    # Event A scores 0 (p=1, YES), event B scores 1 (p=1, NO), one row each.
    # A resample of two events has mean 0 (AA, prob 1/4), 0.5 (AB or BA) or 1 (BB, prob 1/4),
    # so the 2.5% and 97.5% percentiles are 0 and 1.
    rows = [frow(1.0, 0.5, True, event="A"), frow(1.0, 0.5, False, event="B")]
    interval = brier_interval(rows, seed=11)
    assert interval.estimate == pytest.approx(0.5)
    assert (interval.lower, interval.upper) == (0.0, 1.0)


def test_one_cluster_has_no_interval():
    rows = [frow(0.7, 0.5, True, event="A", market=f"A-{m}") for m in range(21)]
    for interval in (brier_interval(rows, seed=1), skill_interval(rows, seed=1)):
        assert interval.lower is None and interval.upper is None
        assert interval.estimate is not None
        assert (interval.n_clusters, interval.n_rows) == (1, 21)


def test_skill_interval_brackets_its_estimate():
    interval = skill_interval(correlated_rows(), seed=5)
    assert interval.lower <= interval.estimate <= interval.upper


def test_pnl_interval_resamples_events_of_settled_trades():
    trades = [
        # Event A: buy 10 YES at 0.40 with fee 0.17, YES wins: 10 - 4.00 - 0.17 = +5.83.
        trade(Side.YES, "10", "0.40", "0.17", result=Side.YES, event="A", market="A-1"),
        # Event B: buy 10 YES at 0.60 with fee 0.17, NO wins: 0 - 6.00 - 0.17 = -6.17.
        trade(Side.YES, "10", "0.60", "0.17", result=Side.NO, event="B", market="B-1"),
        # Still open: not in the P&L and not a row.
        trade(Side.NO, "5", "0.50", "0.09", result=None, event="C", market="C-1"),
    ]
    interval = pnl_interval(trades, seed=2)
    # Total 5.83 - 6.17 = -0.34. Resamples: AA = 11.66, AB = -0.34, BB = -12.34.
    assert interval.estimate == Decimal("-0.34")
    assert (interval.n_clusters, interval.n_rows) == (2, 2)
    assert (interval.lower, interval.upper) == (Decimal("-12.34"), Decimal("11.66"))
    assert isinstance(interval.lower, Decimal)


def test_pnl_interval_with_one_event_has_no_bounds():
    trades = [trade(Side.YES, "10", "0.40", "0.17", result=Side.YES)]
    interval = pnl_interval(trades, seed=2)
    assert interval.estimate == Decimal("5.83")
    assert interval.lower is None and interval.upper is None
