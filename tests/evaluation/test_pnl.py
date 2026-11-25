"""P&L after fees, the equity curve, and drawdown on a worked example."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmfa.evaluation.pnl import (
    EquityPoint,
    equity_curve,
    max_drawdown,
    pnl_summary,
    total_pnl,
    trade_cost,
    trade_payout,
    trade_pnl,
)
from pmfa.evaluation.rows import TradeRow
from pmfa.records.market import Side

from .factories import T0, trade

D = Decimal
BANKROLL = D("1000")

# The worked example. Fees are the exchange's taker fee, 0.07 x C x P x (1 - P), rounded up:
#   T1 buys 10 YES at 0.40, fee 0.07*10*0.4*0.6 = 0.168 -> 0.17. Cost 4.00 + 0.17 = 4.17.
#      YES wins: payout 10.00, P&L +5.83.
#   T2 buys 20 NO at 0.30, fee 0.07*20*0.3*0.7 = 0.294 -> 0.30. Cost 6.00 + 0.30 = 6.30.
#      YES wins, so NO loses: payout 0, P&L -6.30.
#   T3 buys 5 YES at 0.50, fee 0.07*5*0.5*0.5 = 0.0875 -> 0.09. Cost 2.59. Still open.
TRADES = [
    trade(
        Side.YES,
        "10",
        "0.40",
        "0.17",
        result=Side.YES,
        event="A",
        market="A-1",
        filled_hours=0,
        settled_hours=24,
        episode="ep-1",
    ),
    trade(
        Side.NO,
        "20",
        "0.30",
        "0.30",
        result=Side.YES,
        event="B",
        market="B-1",
        filled_hours=1,
        settled_hours=48,
        episode="ep-2",
    ),
    trade(
        Side.YES,
        "5",
        "0.50",
        "0.09",
        result=None,
        event="A",
        market="A-2",
        filled_hours=2,
        episode="ep-3",
    ),
]


def test_cost_payout_and_pnl_of_each_trade():
    t1, t2, t3 = TRADES
    assert (trade_cost(t1), trade_payout(t1), trade_pnl(t1)) == (D("4.17"), D("10"), D("5.83"))
    assert (trade_cost(t2), trade_payout(t2), trade_pnl(t2)) == (D("6.30"), D("0"), D("-6.30"))
    assert (trade_cost(t3), trade_payout(t3), trade_pnl(t3)) == (D("2.59"), None, None)


def test_pnl_per_market_per_event_and_total():
    summary = pnl_summary(TRADES)
    # Total 5.83 - 6.30 = -0.47: the fees (0.47) turned a break-even pair of trades into a loss.
    assert summary.total == D("-0.47")
    assert summary.by_market == {"A-1": D("5.83"), "B-1": D("-6.30")}
    assert summary.by_event == {"A": D("5.83"), "B": D("-6.30")}
    assert summary.fees == D("0.47")
    assert (summary.n_trades, summary.n_settled) == (3, 2)
    assert summary.open_cost == D("2.59")
    assert total_pnl(TRADES) == D("-0.47")


def test_not_trading_earns_zero():
    assert total_pnl([]) == 0
    assert equity_curve([], BANKROLL) == []


def test_equity_curve_steps_at_fills_and_settlements():
    curve = equity_curve(TRADES, BANKROLL)
    steps = [(p.at - T0, p.kind, p.market_ticker, p.cash, p.equity) for p in curve]
    h = timedelta(hours=1)
    assert steps == [
        # Fill T1: cash 1000 - 4.17; equity loses only the fee, 1000 - 0.17.
        (0 * h, "fill", "A-1", D("995.83"), D("999.83")),
        # Fill T2: cash - 6.30; equity - 0.30.
        (1 * h, "fill", "B-1", D("989.53"), D("999.53")),
        # Fill T3: cash - 2.59; equity - 0.09.
        (2 * h, "fill", "A-2", D("986.94"), D("999.44")),
        # T1 settles YES: cash + 10.00; equity + (10.00 - 4.00).
        (24 * h, "settlement", "A-1", D("996.94"), D("1005.44")),
        # T2 settles against it: cash + 0; equity - 6.00.
        (48 * h, "settlement", "B-1", D("996.94"), D("999.44")),
    ]
    # Equity = bankroll + realized P&L - the open trade's fee: 1000 - 0.47 - 0.09.
    # Cash = equity - the open trade's contracts at cost: 999.44 - 2.50.
    assert curve[-1].equity == BANKROLL + pnl_summary(TRADES).total - D("0.09")
    assert curve[-1].cash == curve[-1].equity - D("2.50")


def test_max_drawdown_of_the_worked_example():
    drawdown = max_drawdown(equity_curve(TRADES, BANKROLL), BANKROLL)
    # Peak 1005.44 after T1 settles, trough 999.44 after T2: 6.00 dollars, 6.00 / 1005.44.
    assert drawdown.dollars == D("6.00")
    assert drawdown.fraction == D("6.00") / D("1005.44")
    assert float(drawdown.fraction) == pytest.approx(0.0059675, abs=1e-7)


def test_drawdown_fraction_and_dollars_are_maximized_separately():
    def point(hours: int, equity: str) -> EquityPoint:
        return EquityPoint(
            at=T0 + timedelta(hours=hours),
            kind="settlement",
            episode_id="ep",
            market_ticker="M",
            cash=D(equity),
            equity=D(equity),
        )

    # 100 -> 85 is a 15% fall of 15 dollars; 200 -> 180 is a 10% fall of 20 dollars.
    curve = [point(1, "85"), point(2, "200"), point(3, "180")]
    drawdown = max_drawdown(curve, D("100"))
    assert drawdown.fraction == D("0.15")
    assert drawdown.dollars == D("20")


def test_a_rising_curve_has_no_drawdown():
    winner = trade(Side.YES, "10", "0.40", "0", result=Side.YES)
    drawdown = max_drawdown(equity_curve([winner], BANKROLL), BANKROLL)
    assert (drawdown.fraction, drawdown.dollars) == (0, 0)


def test_trade_rows_reject_inconsistent_settlement():
    with pytest.raises(ValueError, match="set together"):
        TradeRow.model_validate(
            {**TRADES[0].model_dump(), "settled_at": None},
        )
    with pytest.raises(ValueError, match="before it fills"):
        TradeRow.model_validate(
            {**TRADES[0].model_dump(), "settled_at": T0 - timedelta(hours=1)},
        )
