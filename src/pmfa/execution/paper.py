"""The paper executor: simulated immediate-or-cancel limit orders against a book.

An order walks the offers on its side from the cheapest level up to the limit
price, takes whole contracts only, records one fill per level, and pays the
taker fee on each fill. Whatever is left when the walk stops is dropped, as an
immediate-or-cancel order's remainder would be. Nothing ever rests, so there is
nothing to cancel.

Which book an order meets is the caller's choice: the live book in shadow
mode, the recorded execution-time book in replay, a candle-derived book in
backtests. Balance and positions are rebuilt from the stored fills and
settlements (``pmfa.execution.portfolio``), so the state store is the only
record of the paper account.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from pmfa.config import Config
from pmfa.decision import series_fee
from pmfa.execution import portfolio
from pmfa.execution.portfolio import Account
from pmfa.ports import Clock, Exchange, StateStore
from pmfa.records.base import ZERO, stable_id
from pmfa.records.decision import Fill, OrderIntent, OrderResult, OrderStatus
from pmfa.records.market import Balance, OrderBook, Position, Series, Side

BookSource = Callable[[str], OrderBook]
"""Market ticker -> the order book an order sent now would meet."""


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
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.exchange = exchange
        self.books = books
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
        return self.books(ticker)

    def place(self, intent: OrderIntent) -> OrderResult:
        """Fill ``intent`` against the execution book, record it, and mark equity.

        An order whose ``client_order_id`` is already stored is returned as it
        was, so a retry cannot double it. An order sent while the persisted
        halt flag is set is refused (``REJECTED``) and not stored, so the same
        intent can be placed after a resume.
        """
        for existing in self.store.list_orders():
            if existing.client_order_id == intent.client_order_id:
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
            return OrderResult(
                **base, status=OrderStatus.REJECTED, error=f"halted: {halt.reason}"
            )
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

    def cancel_all(self) -> int:
        """Nothing rests on a paper account, so nothing is cancelled."""
        return 0

    # -- beyond the port

    def settle(self, market_ticker: str, result: Side, at: datetime) -> None:
        """Record a market's settlement and mark equity. The outcome resolver calls this."""
        self.store.record_settlement(market_ticker, result, at)
        self.mark(at)

    def account(self, as_of: datetime | None = None) -> Account:
        """The paper account rebuilt from the stored fills and settlements."""
        return portfolio.account(
            self.store.list_fills(),
            self.store.list_settlements(),
            bankroll=self.bankroll,
            as_of=as_of,
        )

    def mark(self, at: datetime | None = None) -> Decimal:
        """Record an equity point (at ``at``, else now) and return the equity."""
        equity = self.account().equity
        self.store.record_equity(at if at is not None else self.clock.now(), equity)
        return equity

    # -- internals

    def _walk(self, intent: OrderIntent, order_id: str, now: datetime) -> list[Fill]:
        book = self.books(intent.market_ticker)
        series = self._series(intent.market_ticker)
        remaining = intent.contracts
        fills: list[Fill] = []
        for level in sorted(book.asks(intent.side), key=lambda lvl: lvl.price):
            if remaining <= 0 or level.price > intent.limit_price:
                break
            take = min(remaining, int(level.contracts))
            if take <= 0:
                continue
            fills.append(
                Fill(
                    fill_id=stable_id("paper-fill", order_id, len(fills)),
                    order_id=order_id,
                    market_ticker=intent.market_ticker,
                    side=intent.side,
                    contracts=Decimal(take),
                    price=level.price,
                    fee=series_fee(take, level.price, series, taker=True),
                    filled_at=now,
                    simulated=True,
                )
            )
            remaining -= take
        return fills

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


__all__ = ["BookSource", "PaperExecutor"]
