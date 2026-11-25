"""The paper executor: simulated immediate-or-cancel limit orders against a book.

An order walks the offers on its side from the cheapest level up to the limit
price, takes whole contracts only, records one fill per level, and pays the
taker fee, rounded up to the cent once for the whole order. Whatever is left
when the walk stops is dropped, as an
immediate-or-cancel order's remainder would be. Nothing ever rests, so there is
nothing to cancel.

Two books, both the caller's choice. ``books`` is what ``execution_book``
returns, the book the pipeline decides with when it re-reads the price before
sending: the live book in shadow mode, the recorded execution-time book in
replay, and the as-of book in backtests (nothing priced after the as-of time).
``fill_books`` is what an order actually fills against, and defaults to
``books``. Only backtests set it apart: there an order fills against the first
one-minute candle after the as-of time plus the latency
(``pmfa.world.snapshot.book``), which the decision never sees.

Balance and positions are rebuilt from the stored fills and settlements
(``pmfa.execution.portfolio``), so the state store is the only record of the
paper account. ``balance().equity`` carries holdings at cost (the bankroll
sizing uses); the equity points written for the daily loss stop and the
drawdown halt (``mark``, at every fill and settlement) carry them at their own
side's current bid from ``books``, or at cost when there is no bid, so the
stops see unrealized losses.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from pmfa.config import Config
from pmfa.decision import exact_fee, round_up_cent
from pmfa.errors import ExchangeError, NotFound
from pmfa.execution import portfolio
from pmfa.execution.portfolio import Account
from pmfa.ports import Clock, Exchange, StateStore
from pmfa.records.base import ZERO, stable_id
from pmfa.records.decision import Fill, OrderIntent, OrderResult, OrderStatus
from pmfa.records.market import Balance, OrderBook, Position, Series, Side

BookSource = Callable[[str], OrderBook]
"""Market ticker -> an order book as of now."""


class PaperExecutor:
    """The ``Executor`` port with simulated fills. ``simulated`` is always True."""

    simulated: bool = True

    def __init__(
        self,
        store: StateStore,
        clock: Clock,
        config: Config,
        exchange: Exchange,
        books: BookSource,
        *,
        bankroll: Decimal | None = None,
        fill_books: BookSource | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.exchange = exchange
        self.books = books
        self.fill_books = fill_books if fill_books is not None else books
        self.bankroll = Decimal(
            bankroll if bankroll is not None else config.execution.notional_bankroll
        )
        self._series_of_market: dict[str, Series] = {}
        self._event_of_market: dict[str, str] = {}

    # -- the Executor port

    def balance(self) -> Balance:
        acct = self.account()
        return Balance(cash=acct.cash, equity=acct.equity)

    def positions(self, event_ticker: str | None = None) -> list[Position]:
        return portfolio.positions(self.account(), self._event_of, event_ticker=event_ticker)

    def execution_book(self, ticker: str) -> OrderBook:
        """The book the pipeline decides with at execution (``books``; see the module docstring)."""
        return self.books(ticker)

    def place(self, intent: OrderIntent) -> OrderResult:
        """Fill ``intent`` against the fill book, record it, and mark equity.

        An order whose ``client_order_id`` is already stored is returned as it
        was, so a retry cannot double it. An order sent while the persisted
        halt flag is set is refused (``REJECTED``) and not stored, so the same
        intent can be placed after a resume.
        """
        existing = self.find_order(intent)
        if existing is not None:
            return existing

        now = self.clock.now()
        order_id = stable_id("paper-order", intent.client_order_id)
        base = {
            "order_id": order_id,
            "client_order_id": intent.client_order_id,
            "episode_id": intent.episode_id,
            "market_ticker": intent.market_ticker,
            "side": intent.side,
            "requested_contracts": intent.contracts,
            "limit_price": intent.limit_price,
            "placed_at": now,
        }
        halt = self.store.get_halt()
        if halt.halted:
            return OrderResult(**base, status=OrderStatus.REJECTED, error=f"halted: {halt.reason}")
        if intent.contracts <= 0:
            result = OrderResult(
                **base, status=OrderStatus.REJECTED, error="an order needs at least one contract"
            )
            self.store.save_order(result)
            return result

        fills = self._walk(intent, order_id, now)
        filled = sum((f.contracts for f in fills), ZERO)
        if filled == 0:
            status = OrderStatus.UNFILLED
        elif filled < intent.contracts:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.FILLED
        result = OrderResult(**base, status=status, fills=fills)
        self.store.save_order(result)
        self.mark(now)
        return result

    def find_order(self, intent: OrderIntent) -> OrderResult | None:
        """The stored order with ``intent``'s client order id, if there is one. Places nothing.

        The store is the paper account's only record, so an order this executor
        filled is found here even when the caller crashed before seeing the result.
        """
        for existing in self.store.list_orders():
            if existing.client_order_id == intent.client_order_id:
                return existing
        return None

    def cancel_all(self) -> int:
        """Nothing rests on a paper account, so nothing is cancelled."""
        return 0

    # -- beyond the port

    def settle(self, market_ticker: str, result: Side, at: datetime) -> None:
        """Record a market's settlement and mark equity. The outcome resolver calls this.

        The settlement is recorded at ``at``, but the equity point at
        ``max(at, now)``: the mark holds the whole account as it stands now, so
        writing it under a past time would leave a later point (from another
        settlement or fill) as the "current" equity without this one.
        """
        self.store.record_settlement(market_ticker, result, at)
        self.mark(max(at, self.clock.now()))

    def account(self, as_of: datetime | None = None) -> Account:
        """The paper account rebuilt from the stored fills and settlements."""
        return portfolio.account(
            self.store.list_fills(),
            self.store.list_settlements(),
            bankroll=self.bankroll,
            as_of=as_of,
        )

    def mark(self, at: datetime | None = None) -> Decimal:
        """Record an equity point (at ``at``, else now) and return its equity.

        The equity is marked (``portfolio.marked_equity``): open holdings at
        their own side's current bid in ``books``, at cost without one. These
        points are what the daily loss stop and the drawdown halt read.
        """
        equity = portfolio.marked_equity(self.account(), self._bid)
        self.store.record_equity(at if at is not None else self.clock.now(), equity)
        return equity

    # -- internals

    def _bid(self, market_ticker: str, side: Side) -> Decimal | None:
        """The best bid for ``side`` now, or ``None`` when there is none or it cannot be read."""
        try:
            book = self.books(market_ticker)
        except (ExchangeError, NotFound):
            return None
        prices = [lvl.price for lvl in book.bids(side) if lvl.contracts > 0]
        return max(prices) if prices else None

    def _walk(self, intent: OrderIntent, order_id: str, now: datetime) -> list[Fill]:
        """Take whole contracts up to the limit. A level offers floor(its contracts)."""
        book = self.fill_books(intent.market_ticker)
        series = self._series(intent.market_ticker)
        remaining = intent.contracts
        taken: list[tuple[Decimal, int]] = []
        for level in sorted(book.asks(intent.side), key=lambda lvl: lvl.price):
            if remaining <= 0 or level.price > intent.limit_price:
                break
            take = min(remaining, math.floor(level.contracts))
            if take <= 0:
                continue
            taken.append((level.price, take))
            remaining -= take
        fees = _order_fees(taken, series)
        return [
            Fill(
                fill_id=stable_id("paper-fill", order_id, i),
                order_id=order_id,
                market_ticker=intent.market_ticker,
                side=intent.side,
                contracts=Decimal(take),
                price=price,
                fee=fee,
                filled_at=now,
                simulated=True,
            )
            for i, ((price, take), fee) in enumerate(zip(taken, fees, strict=True))
        ]

    def _series(self, market_ticker: str) -> Series:
        if market_ticker not in self._series_of_market:
            event = self.exchange.get_event(self._event_of(market_ticker))
            self._series_of_market[market_ticker] = self.exchange.get_series(event.series_ticker)
        return self._series_of_market[market_ticker]

    def _event_of(self, market_ticker: str) -> str:
        if market_ticker not in self._event_of_market:
            self._event_of_market[market_ticker] = self.exchange.get_market(
                market_ticker
            ).event_ticker
        return self._event_of_market[market_ticker]


def _order_fees(taken: list[tuple[Decimal, int]], series: Series) -> list[Decimal]:
    """Taker fees for the fills of one order.

    The order's total fee is rounded up to the cent once, as sizing assumes, and
    not once per price level. Each fill carries its own share; the last fill
    takes the rounding remainder so the fills sum to the order's fee.
    """
    if not taken:
        return []
    exact = [
        exact_fee(take, price, series.fee_type, series.fee_multiplier, taker=True)
        for price, take in taken
    ]
    total = round_up_cent(sum(exact, Decimal("0")))
    fees = [fee.quantize(Decimal("0.0001"), rounding=ROUND_DOWN) for fee in exact]
    fees[-1] += total - sum(fees, Decimal("0"))
    return fees


__all__ = ["BookSource", "PaperExecutor"]
