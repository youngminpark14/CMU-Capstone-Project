"""Each stage benchmark exactly at, and just beyond, its threshold."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmfa.config import StageConfig
from pmfa.evaluation.benchmarks import approval_benchmark, backtest_benchmark, shadow_benchmark
from pmfa.evaluation.rows import ForecastRow, TradeRow
from pmfa.records.decision import ReasonCode, Review, ReviewDecision
from pmfa.records.market import Side

from .factories import T0, frow, trade

STAGES = StageConfig()  # 200 markets; 4 weeks, drawdown under 10%; 100 trades, under 15%.
D = Decimal


# Backtest: Brier skill above zero on at least 200 markets.


def market_rows(n_markets: int, probability: float = 0.7) -> list[ForecastRow]:
    """``n_markets`` markets, five per event, priced 0.6 and resolved YES."""
    return [
        frow(probability, 0.6, True, event=f"E{i // 5}", market=f"E{i // 5}-{i % 5}")
        for i in range(n_markets)
    ]


def test_backtest_met_at_exactly_200_markets():
    # p = 0.7 vs price 0.6 on YES: agent 0.09, market 0.16, skill 1 - 0.09/0.16 = 0.4375.
    result = backtest_benchmark(market_rows(200), stages=STAGES)
    assert result.met
    assert result.skill is not None and abs(result.skill - 0.4375) < 1e-12
    assert (result.n_markets, result.n_events, result.min_markets) == (200, 40, 200)
    assert result.shortfalls == []


def test_backtest_not_met_at_199_markets():
    result = backtest_benchmark(market_rows(199), stages=STAGES)
    assert not result.met
    assert result.n_markets == 199
    assert len(result.shortfalls) == 1


def test_backtest_skill_exactly_zero_is_not_above_zero():
    # Forecasting the market price everywhere: skill exactly 0.
    result = backtest_benchmark(market_rows(200, probability=0.6), stages=STAGES)
    assert result.skill == 0.0
    assert not result.met


def test_backtest_skill_just_above_zero_is_met():
    # One market at 0.61 instead of 0.6: its error 0.1521 < 0.16, so skill is just positive.
    rows = [*market_rows(199, probability=0.6), frow(0.61, 0.6, True, event="X", market="X-1")]
    result = backtest_benchmark(rows, stages=STAGES)
    assert result.skill is not None and 0 < result.skill < 1e-3
    assert result.met


def test_backtest_abstentions_neither_count_as_markets_nor_help_skill():
    rows = [*market_rows(199), frow(None, 0.6, True, event="X", market="X-1")]
    result = backtest_benchmark(rows, stages=STAGES)
    assert result.n_markets == 199
    assert not result.met


def test_backtest_threshold_comes_from_config():
    assert backtest_benchmark(market_rows(10), stages=StageConfig(backtest_min_markets=10)).met


# Shadow: four weeks, positive P&L after fees, drawdown under 10%.

WINNER = trade(Side.YES, "10", "0.40", "0.17", result=Side.YES)  # +5.83


def shadow(trades: list[TradeRow], days: float = 28) -> object:
    return shadow_benchmark(
        trades, bankroll=D("1000"), start=T0, end=T0 + timedelta(days=days), stages=STAGES
    )


def test_shadow_met_at_exactly_four_weeks():
    result = shadow([WINNER])
    assert result.met
    assert result.weeks == 4.0
    assert result.total_pnl == D("5.83")
    # The only fall is the fee at the fill: 0.17 / 1000.
    assert result.max_drawdown_fraction == D("0.00017")


def test_shadow_not_met_one_second_short_of_four_weeks():
    result = shadow([WINNER], days=28 - 1 / 86400)
    assert not result.met
    assert len(result.shortfalls) == 1


def test_shadow_pnl_exactly_zero_is_not_positive():
    # Win 10 YES at 0.50 (+5.00) and lose 10 YES at 0.50 (-5.00), no fees: total 0.
    trades = [
        trade(Side.YES, "10", "0.50", "0", result=Side.YES, market="A-1"),
        trade(Side.YES, "10", "0.50", "0", result=Side.NO, market="A-2"),
    ]
    result = shadow(trades)
    assert result.total_pnl == 0
    assert not result.met


def test_shadow_pnl_one_cent_positive_is_met():
    # Win 10 YES at 0.499 (+5.01) and lose 10 YES at 0.50 (-5.00): total +0.01.
    trades = [
        trade(Side.YES, "10", "0.499", "0", result=Side.YES, market="A-1"),
        trade(Side.YES, "10", "0.50", "0", result=Side.NO, market="A-2"),
    ]
    result = shadow(trades)
    assert result.total_pnl == D("0.01")
    assert result.met


def drawdown_then_recovery(losing_contracts: str) -> list[TradeRow]:
    """A loss of ``contracts x 0.50`` from the 1000 peak, then a +200 winner settling later."""
    return [
        trade(Side.YES, losing_contracts, "0.50", "0", result=Side.NO, market="A-1", event="A"),
        trade(
            Side.YES,
            "400",
            "0.50",
            "0",
            result=Side.YES,
            market="B-1",
            event="B",
            filled_hours=30,
            settled_hours=60,
        ),
    ]


def test_shadow_drawdown_of_exactly_ten_percent_is_not_under():
    # 200 contracts at 0.50 lost: 1000 -> 900, a fall of 100 / 1000 = 10%.
    result = shadow(drawdown_then_recovery("200"))
    assert result.max_drawdown_fraction == D("0.1")
    assert result.max_drawdown_dollars == D("100")
    assert result.total_pnl == D("100")  # -100 + 200
    assert not result.met


def test_shadow_drawdown_just_under_ten_percent_is_met():
    # 199 contracts at 0.50 lost: a fall of 99.50 / 1000 = 9.95%.
    result = shadow(drawdown_then_recovery("199"))
    assert result.max_drawdown_fraction == D("0.0995")
    assert result.met


# Approval: at least 100 approved trades, under 15% overridden.


def decisions(n: int, overridden: int) -> list[ReviewDecision]:
    resized = overridden // 2
    rejected = overridden - resized
    return (
        [ReviewDecision.APPROVE] * (n - overridden)
        + [ReviewDecision.RESIZE] * resized
        + [ReviewDecision.REJECT] * rejected
    )


def test_approval_met_at_100_trades_with_14_overridden():
    result = approval_benchmark(decisions(100, 14), stages=STAGES)
    assert result.met
    assert (result.n_trades, result.n_overridden, result.override_fraction) == (100, 14, 0.14)


def test_approval_exactly_15_percent_overridden_is_not_under():
    # 0.15 x 100 is 15.000000000000002 in floating point; the comparison must be exact.
    result = approval_benchmark(decisions(100, 15), stages=STAGES)
    assert not result.met
    assert len(result.shortfalls) == 1


def test_approval_not_met_at_99_trades():
    result = approval_benchmark(decisions(99, 0), stages=STAGES)
    assert not result.met
    assert result.n_trades == 99


def test_approval_accepts_review_records():
    reviews = [
        Review(proposal_id=f"p-{i}", decision=ReviewDecision.APPROVE, decided_at=T0)
        for i in range(99)
    ] + [
        Review(
            proposal_id="p-99",
            decision=ReviewDecision.REJECT,
            reason_code=ReasonCode.DISAGREE_WITH_FORECAST,
            decided_at=T0,
        )
    ]
    result = approval_benchmark(reviews, stages=STAGES)
    assert (result.n_trades, result.n_overridden, result.met) == (100, 1, True)


def test_approval_with_no_reviews():
    result = approval_benchmark([], stages=STAGES)
    assert not result.met
    assert result.override_fraction is None
