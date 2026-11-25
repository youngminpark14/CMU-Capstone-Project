"""Portfolio accounting on a worked example with fees.

Bankroll 1000. Fees are the taker fees rounded up to the cent.

1. Buy YES 10 @ 0.40, fee 0.17: cash 995.83, YES 10 costing 4.00.
2. Buy YES 10 @ 0.50, fee 0.18: cash 990.65, YES 20 costing 9.00 (avg 0.45).
3. Buy NO 15 @ 0.52, fee 0.27: pays 8.07, closes 15 pairs (+15): cash 997.58;
   YES 5 left at avg 0.45 (2.25); equity 999.83.
4. Buy NO 8 @ 0.55, fee 0.14: pays 4.54, closes 5 pairs (+5): cash 998.04;
   NO 3 left at 0.55 (1.65); equity 999.69.
5. The market settles NO: +3. Cash and equity 1001.04, nothing held.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmfa.execution.portfolio import account, positions
from pmfa.records.market import Side
from tests.world.recorded.factories import AS_OF, EVENT, HOLD

from .factories import fill

T = [AS_OF + timedelta(minutes=i) for i in range(6)]
FILLS = [
    fill(Side.YES, 10, "0.40", "0.17", T[0]),
    fill(Side.YES, 10, "0.50", "0.18", T[1]),
    fill(Side.NO, 15, "0.52", "0.27", T[2]),
    fill(Side.NO, 8, "0.55", "0.14", T[3]),
]
BANKROLL = Decimal("1000")


def at(i: int):
    return account(FILLS, [], bankroll=BANKROLL, as_of=T[i])


def test_same_side_fills_average_their_cost() -> None:
    acct = at(1)
    assert acct.cash == Decimal("990.65")
    [h] = acct.holdings
    assert (h.side, h.contracts, h.cost, h.avg_price) == (
        Side.YES,
        20,
        Decimal("9.00"),
        Decimal("0.45"),
    )
    assert acct.equity == Decimal("999.65")


def test_buying_the_other_side_closes_pairs_for_a_dollar_each() -> None:
    acct = at(2)
    assert acct.cash == Decimal("997.58")
    assert acct.netted == Decimal("15")
    [h] = acct.holdings
    assert (h.side, h.contracts, h.cost) == (Side.YES, 5, Decimal("2.25"))
    assert acct.equity == Decimal("999.83")


def test_netting_past_zero_flips_the_holding() -> None:
    acct = at(3)
    assert acct.cash == Decimal("998.04")
    [h] = acct.holdings
    assert (h.side, h.contracts, h.cost) == (Side.NO, 3, Decimal("1.65"))
    assert acct.equity == Decimal("999.69")
    [p] = positions(acct, lambda _: EVENT)
    assert (p.market_ticker, p.side, p.contracts, p.avg_price) == (
        HOLD,
        Side.NO,
        3,
        Decimal("0.55"),
    )
    assert positions(acct, lambda _: EVENT, event_ticker="OTHER") == []


def test_settlement_pays_the_winning_side() -> None:
    acct = account(FILLS, [(HOLD, Side.NO, T[4])], bankroll=BANKROLL)
    assert acct.holdings == []
    assert acct.payouts == Decimal("3")
    assert acct.fees == Decimal("0.76")
    assert acct.cash == acct.equity == Decimal("1001.04")


def test_settlement_on_the_losing_side_pays_nothing() -> None:
    acct = account(FILLS, [(HOLD, Side.YES, T[4])], bankroll=BANKROLL)
    assert acct.payouts == 0
    assert acct.cash == acct.equity == Decimal("998.04")


def test_fills_are_applied_in_time_order_whatever_the_input_order() -> None:
    assert account(list(reversed(FILLS)), [], bankroll=BANKROLL) == account(
        FILLS, [], bankroll=BANKROLL
    )
