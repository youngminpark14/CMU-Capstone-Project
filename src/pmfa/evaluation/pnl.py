"""Profit and loss after fees, the equity curve, and maximum drawdown.

All money is decimal. A trade costs ``contracts x price + fee`` when it fills and
pays one dollar per contract when its side wins at settlement, nothing when it
loses. P&L is realized: only settled trades count toward it. Open trades are
reported separately by what they cost.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pmfa.execution.portfolio import account
from pmfa.records.base import ONE, ZERO, Dollars, Record, UtcTime
from pmfa.records.decision import Fill
from pmfa.records.market import Side

from .rows import TradeRow


class PnlSummary(Record):
    """Realized P&L after fees, per market, per event, and in total.

    ``total`` is also the P&L against not trading: not trading earns exactly
    zero, so the difference between trading and abstaining is the total itself.
    """

    total: Dollars
    by_market: dict[str, Dollars]
    by_event: dict[str, Dollars]
    fees: Dollars
    """Fees paid on the settled trades (already inside ``total``)."""
    n_trades: int
    n_settled: int
    open_cost: Dollars
    """What the still-open trades cost, fees included. Not in ``total``."""


class EquityPoint(Record):
    """The account at one moment, after every fill and settlement at that moment.

    ``cash`` and ``equity`` are the paper account's (``pmfa.execution.portfolio``):
    cash is the bankroll less what was paid for fills, plus the dollar returned
    by each YES/NO pair netted in one market, plus what settlements paid out.
    ``equity`` carries open holdings at what their contracts cost, so a fill
    lowers equity only by its fee (and a netting fill by the pairs' gain or
    loss) and a settlement moves it by the holding's result. There are no
    price marks here.

    When several changes share a moment (the markets of one event settling
    together, say), there is one point for all of them; ``kind``,
    ``episode_id`` and ``market_ticker`` are those of the last change applied.
    """

    at: UtcTime
    kind: Literal["fill", "settlement"]
    episode_id: str
    market_ticker: str
    cash: Dollars
    equity: Dollars


class Drawdown(Record):
    """The largest fall of equity from a running peak, as a fraction of that peak and in dollars.

    The two are maximized separately, so they can come from different troughs.
    """

    fraction: Decimal
    dollars: Dollars


def trade_cost(trade: TradeRow) -> Decimal:
    """Cash paid at the fill: ``contracts x price + fee``."""
    return trade.contracts * trade.price + trade.fee


def trade_payout(trade: TradeRow) -> Decimal | None:
    """One dollar per contract when the trade's side won, zero when it lost, ``None`` while open."""
    if trade.result is None:
        return None
    return trade.contracts * ONE if trade.result == trade.side else ZERO


def trade_pnl(trade: TradeRow) -> Decimal | None:
    """Realized P&L after fees of one trade; ``None`` while it is open."""
    payout = trade_payout(trade)
    return None if payout is None else payout - trade_cost(trade)


def pnl_summary(trades: Sequence[TradeRow]) -> PnlSummary:
    """Realized P&L after fees per market, per event, and in total.

    The total is the P&L against not trading, since not trading earns zero.
    """
    by_market: dict[str, Decimal] = defaultdict(lambda: ZERO)
    by_event: dict[str, Decimal] = defaultdict(lambda: ZERO)
    total = fees = open_cost = ZERO
    n_settled = 0
    for t in trades:
        pnl = trade_pnl(t)
        if pnl is None:
            open_cost += trade_cost(t)
            continue
        n_settled += 1
        by_market[t.market_ticker] += pnl
        by_event[t.event_ticker] += pnl
        total += pnl
        fees += t.fee
    return PnlSummary(
        total=total,
        by_market=dict(by_market),
        by_event=dict(by_event),
        fees=fees,
        n_trades=len(trades),
        n_settled=n_settled,
        open_cost=open_cost,
    )


def equity_curve(trades: Sequence[TradeRow], bankroll: Decimal) -> list[EquityPoint]:
    """The account at every distinct fill or settlement time, in time order, from ``bankroll``.

    Each point is the paper account's own accounting
    (``pmfa.execution.portfolio.account(..., as_of=t)``) over the trades as
    fills and their markets' settlements, so the curve and the account cannot
    disagree. Cash falls by a trade's cost at its fill and rises by the payout
    at settlement; equity carries open holdings at cost, so it falls by the fee
    at a fill and moves by the result at settlement. YES and NO in one market
    net as they do on the account: buying the other side closes contracts one
    for one and realizes a dollar per closed pair at that fill, so a hedge
    whose prices do not sum to one shows its gain or loss when the second leg
    fills, not at settlement. Every change at one time is applied before that
    time's single point is taken: a winning and a losing leg settling together
    never shows a trough that did not exist. Once everything settles, equity
    equals cash.
    """
    fills = [
        Fill(
            fill_id=f"trade-{i}",
            order_id=f"trade-{i}",
            market_ticker=t.market_ticker,
            side=t.side,
            contracts=t.contracts,
            price=t.price,
            fee=t.fee,
            filled_at=t.filled_at,
            simulated=True,
        )
        for i, t in enumerate(trades)
    ]
    settled: dict[str, tuple[str, Side, UtcTime]] = {}
    for t in trades:
        if t.result is None or t.settled_at is None:
            continue
        known = settled.get(t.market_ticker)
        if known is None or t.settled_at < known[2]:
            settled[t.market_ticker] = (t.market_ticker, t.result, t.settled_at)
    settlements = list(settled.values())

    # The last change applied at each moment names the point: fills first, then
    # settlements, each in input order.
    changes: list[tuple[UtcTime, int, int, TradeRow]] = []
    for i, t in enumerate(trades):
        changes.append((t.filled_at, 0, i, t))
        if t.settled_at is not None:
            changes.append((t.settled_at, 1, i, t))
    changes.sort(key=lambda c: (c[0], c[1], c[2]))
    last: dict[UtcTime, tuple[int, TradeRow]] = {}
    for at, order, _, t in changes:
        last[at] = (order, t)

    points: list[EquityPoint] = []
    for at, (order, t) in last.items():
        acct = account(fills, settlements, bankroll=bankroll, as_of=at)
        points.append(
            EquityPoint(
                at=at,
                kind="fill" if order == 0 else "settlement",
                episode_id=t.episode_id,
                market_ticker=t.market_ticker,
                cash=acct.cash,
                equity=acct.equity,
            )
        )
    return points


def max_drawdown(curve: Sequence[EquityPoint], bankroll: Decimal) -> Drawdown:
    """Maximum drawdown of equity, starting from a peak at ``bankroll``."""
    peak = bankroll
    worst_fraction = worst_dollars = ZERO
    for p in curve:
        peak = max(peak, p.equity)
        fall = peak - p.equity
        worst_dollars = max(worst_dollars, fall)
        if peak > 0:
            worst_fraction = max(worst_fraction, fall / peak)
    return Drawdown(fraction=worst_fraction, dollars=worst_dollars)
