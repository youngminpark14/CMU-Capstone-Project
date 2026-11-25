"""Paper-portfolio accounting: holdings, cash, and equity from fills and settlements.

Pure functions over the stored fills and settlements, so the paper executor's
balance and positions can always be rebuilt from the state store and checked by
hand. The rules follow the exchange:

- **Netting.** YES and NO in the same market net against each other. Buying NO
  while holding YES closes YES contracts one for one, and each closed pair
  returns one dollar of cash at that moment (one of the two sides must win).
- **Average cost.** What remains of a holding keeps its average price; closing
  contracts removes them at that average.
- **Cash** is the starting bankroll, less what fills cost (``price x contracts +
  fee``), plus the dollars returned by netting, plus settlement payouts (one
  dollar per contract on the winning side).
- **Equity** is cash plus open holdings at cost (``contracts x average price``,
  fees excluded), so a fill lowers equity by its fee only. This is the
  bankroll sizing uses (what ``Balance.equity`` documents).
- **Marked equity** (:func:`marked_equity`) is cash plus each open holding at
  the current best bid on its own side, or at cost when that side has no bid.
  It is what the equity points behind the daily loss stop and the drawdown
  halt record, so the stops see unrealized losses. The bid is the conservative
  side: it is what the holding could be sold for now.

Decimal throughout; contracts are kept as the exchange counts them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal

from pmfa.records.base import ONE, PRICE_QUANTUM, ZERO, Count, Dollars, Record
from pmfa.records.decision import Fill
from pmfa.records.market import Position, Side


class Holding(Record):
    """A net holding in one market: contracts on one side and what they cost (fees excluded)."""

    market_ticker: str
    side: Side
    contracts: Count
    cost: Dollars

    @property
    def avg_price(self) -> Decimal:
        return self.cost / self.contracts if self.contracts else ZERO


class Account(Record):
    """The paper account at one moment."""

    bankroll: Dollars
    cash: Dollars
    equity: Dollars
    holdings: list[Holding]
    """Open holdings, one per market with contracts, ordered by market ticker."""
    fees: Dollars
    """Fees paid on every fill so far."""
    netted: Dollars
    """Cash returned by closing YES/NO pairs."""
    payouts: Dollars
    """Cash paid by settlements."""


def account(
    fills: Iterable[Fill],
    settlements: Iterable[tuple[str, Side, datetime]],
    *,
    bankroll: Decimal,
    as_of: datetime | None = None,
) -> Account:
    """Replay fills and settlements in time order into an ``Account``.

    ``as_of`` leaves out anything after that time. At equal times fills come
    before settlements. A fill in a market that has already settled is applied
    to cash only (it cannot happen on the exchange, and it pays nothing).
    """
    events: list[tuple[datetime, int, int, Fill | tuple[str, Side]]] = []
    for n, f in enumerate(fills):
        events.append((f.filled_at, 0, n, f))
    for n, (ticker, result, at) in enumerate(settlements):
        events.append((at, 1, n, (ticker, Side(result))))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    cash = Decimal(bankroll)
    fees = netted = payouts = ZERO
    # market -> (side, contracts, cost)
    book: dict[str, tuple[Side, Decimal, Decimal]] = {}
    settled: set[str] = set()
    for at, _, _, item in events:
        if as_of is not None and at > as_of:
            continue
        if isinstance(item, Fill):
            cash -= item.price * item.contracts + item.fee
            fees += item.fee
            if item.market_ticker in settled:
                continue
            closed, held = _apply_fill(book.get(item.market_ticker), item)
            cash += closed * ONE
            netted += closed * ONE
            if held is None:
                book.pop(item.market_ticker, None)
            else:
                book[item.market_ticker] = held
        else:
            ticker, result = item
            settled.add(ticker)
            held_now = book.pop(ticker, None)
            if held_now is not None and held_now[0] is result:
                cash += held_now[1] * ONE
                payouts += held_now[1] * ONE

    holdings = [
        Holding(market_ticker=t, side=side, contracts=n, cost=cost)
        for t, (side, n, cost) in sorted(book.items())
    ]
    equity = cash + sum((h.cost for h in holdings), ZERO)
    return Account(
        bankroll=bankroll,
        cash=cash,
        equity=equity,
        holdings=holdings,
        fees=fees,
        netted=netted,
        payouts=payouts,
    )


def _apply_fill(
    held: tuple[Side, Decimal, Decimal] | None, fill: Fill
) -> tuple[Decimal, tuple[Side, Decimal, Decimal] | None]:
    """Net one fill against a holding. Returns (pairs closed, the holding after)."""
    bought = Decimal(fill.contracts)
    paid = fill.price * bought
    if held is None or held[1] == 0:
        return ZERO, (fill.side, bought, paid) if bought else None
    side, n, cost = held
    if side is fill.side:
        return ZERO, (side, n + bought, cost + paid)
    closed = min(n, bought)
    remaining_held = n - closed
    remaining_bought = bought - closed
    if remaining_held > 0:
        return closed, (side, remaining_held, cost * remaining_held / n)
    if remaining_bought > 0:
        return closed, (fill.side, remaining_bought, fill.price * remaining_bought)
    return closed, None


def marked_equity(acct: Account, bid: Callable[[str, Side], Decimal | None]) -> Decimal:
    """Cash plus each open holding at its own side's bid, or at cost without one.

    ``bid(market_ticker, side)`` is the best bid for ``side`` in that market now,
    or ``None`` when there is none. The value of a holding is ``contracts x bid``.
    """
    total = acct.cash
    for h in acct.holdings:
        price = bid(h.market_ticker, h.side)
        total += h.cost if price is None else h.contracts * price
    return total


def positions(
    acct: Account, event_of: Callable[[str], str], *, event_ticker: str | None = None
) -> list[Position]:
    """The account's holdings as ``Position`` records, optionally for one event.

    ``event_of`` maps a market ticker to its event ticker. Positions hold whole
    contracts, so a fractional remainder (possible only on a real exchange) is
    floored.
    """
    result = []
    for h in acct.holdings:
        whole = int(h.contracts)
        if whole <= 0:
            continue
        event = event_of(h.market_ticker)
        if event_ticker is not None and event != event_ticker:
            continue
        result.append(
            Position(
                market_ticker=h.market_ticker,
                event_ticker=event,
                side=h.side,
                contracts=whole,
                avg_price=h.avg_price.quantize(PRICE_QUANTUM),
            )
        )
    return result


__all__ = ["Account", "Holding", "account", "marked_equity", "positions"]
