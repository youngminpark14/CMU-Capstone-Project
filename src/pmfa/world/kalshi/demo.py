"""The Kalshi demo exchange: market data from the demo host, and real orders with mock funds.

Everything about the private endpoints here (orders, order groups, fills,
balance, positions) is built from Kalshi's OpenAPI document and its order
guides as of 2026-11-25, not from live calls: no demo credentials existed when
this was written, and the test responses for these endpoints are constructed
from the schemas. Re-check against the demo exchange before relying on it.

What the executor does, per the spec's "Execution and oversight" section:

- **One order type.** :meth:`DemoExecutor.place` sends a single
  immediate-or-cancel limit order through ``POST /portfolio/events/orders``.
  That endpoint quotes everything on the YES leg: ``side="bid"`` buys YES at
  ``price``; ``side="ask"`` sells YES, which is buying NO at ``1 - price``. So
  buying YES at ``p`` is a bid at ``p`` and buying NO at ``p`` is an ask at
  ``1 - p``. Price and count are fixed-point strings.
- **Idempotent retries.** The order carries the intent's deterministic
  ``client_order_id``. When the exchange says ``order_already_exists``, or the
  outcome is unknown (timeout, 408, 5xx), the order is looked up by that id
  instead of being sent again. The exchange only reserves an id while the
  order is open, so the lookup, not a resend, is what prevents a double order.
- **Breaker.** Every order joins an order group whose contracts limit is
  ``config.caps.order_group_contracts_limit`` over the exchange's rolling
  15-second window; the group is found or created once per exchange shard.
  When it trips, the exchange rejects new orders until the group is reset,
  and this executor does not reset it.
- **Reconciliation.** Fills and fees come from ``GET /portfolio/fills`` for
  the order. If that read lags the order response, or fails after the order
  was accepted, the shortfall is recorded at the limit price (never
  understating cost) and the result says so: an accepted order always comes
  back as a result, never as an exception, so it can be stored.
- **Halt.** Given a ``halt_check`` (the state store's ``get_halt``), the
  executor reads the persisted halt flag when an order starts and again
  immediately before the order is sent, and refuses (``REJECTED``, error
  ``halted: ...``) without sending when it is set. ``build_demo_world`` sets it.
- **Lookup.** :meth:`DemoExecutor.find_order` finds the order already placed
  for an intent by its client order id, so a continued episode can reconcile
  after a crash or an exchange error without sending again.
- **Marks.** :meth:`DemoExecutor.mark` values the subaccount as the paper
  executor's marks do: cash plus each position at its own side's best bid in
  the demo book, at cost when there is no bid. Given an ``equity_sink`` (the
  state store's ``record_equity``; ``build_demo_world`` sets it), it records the
  point, which is what the daily loss stop and the drawdown halt read.
- **Rejections.** A refused order carries the deterministic id
  ``stable_id("demo-rejected", client_order_id)``, so distinct rejections are
  stored as distinct orders and count toward the fill-failure halt.
- **Scope.** Balance, positions, orders and cancels are for the configured
  numbered subaccount only (``config.kalshi.demo_subaccount``).

The executor refuses to be built for any host but the demo one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from pmfa.config import Config
from pmfa.errors import ConfigError, ExchangeError, NotFound
from pmfa.ports import Clock
from pmfa.records.base import ONE, ZERO, stable_id
from pmfa.records.decision import Fill, OrderIntent, OrderResult, OrderStatus
from pmfa.records.episode import HaltState
from pmfa.records.market import Balance, OrderBook, Position, Side
from pmfa.world.kalshi.client import KalshiClient, KalshiExchangeError, KalshiNotFound
from pmfa.world.kalshi.data import KalshiData
from pmfa.world.kalshi.parse import (
    count,
    decimal,
    parse_balance,
    parse_fill,
    parse_positions,
    utc,
)
from pmfa.world.kalshi.signing import load_signer

DEMO_HOSTS = frozenset({"external-api.demo.kalshi.co", "demo-api.kalshi.co"})
"""The demo exchange's REST hosts (the second is the older, still supported name)."""

ORDERS_PATH = "/portfolio/events/orders"
PAGE_LIMIT = 1000


def is_demo_url(base_url: str) -> bool:
    parts = urlsplit(base_url)
    return parts.scheme == "https" and parts.netloc.lower() in DEMO_HOSTS


def order_payload(
    intent: OrderIntent, *, subaccount: int, order_group_id: str | None
) -> dict[str, Any]:
    """The ``CreateOrderV2Request`` body for buying ``intent.side``.

    Buying YES at ``p`` is ``side="bid"`` at ``p``; buying NO at ``p`` is
    ``side="ask"`` on the YES leg at ``1 - p``.
    """
    if intent.side is Side.YES:
        book_side, yes_price = "bid", intent.limit_price
    else:
        book_side, yes_price = "ask", ONE - intent.limit_price
    body: dict[str, Any] = {
        "ticker": intent.market_ticker,
        "client_order_id": intent.client_order_id,
        "side": book_side,
        "count": f"{Decimal(intent.contracts):.2f}",
        "price": f"{yes_price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "subaccount": subaccount,
    }
    if order_group_id is not None:
        body["order_group_id"] = order_group_id
    return body


class DemoExecutor:
    """The ``Executor`` port against the Kalshi demo exchange (real orders, mock funds)."""

    simulated = False

    def __init__(
        self,
        client: KalshiClient,
        clock: Clock,
        config: Config,
        *,
        data: KalshiData | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fill_read_attempts: int = 3,
        fill_read_delay_seconds: float = 0.5,
        halt_check: Callable[[], HaltState] | None = None,
        equity_sink: Callable[[datetime, Decimal], None] | None = None,
    ) -> None:
        if not is_demo_url(client.base_url):
            raise ConfigError(
                f"the demo executor only talks to the demo exchange; refusing {client.base_url!r}"
            )
        if client.signer is None:
            raise ConfigError("the demo executor needs a signed client (demo API credentials)")
        self.client = client
        self.clock = clock
        self.config = config
        self.subaccount = config.kalshi.demo_subaccount
        self.data = data or KalshiData(client, clock, config)
        if not is_demo_url(self.data.client.base_url):
            raise ConfigError("the demo executor's order book must come from the demo exchange")
        self._sleep = sleep
        self._fill_read_attempts = max(1, fill_read_attempts)
        self._fill_read_delay = fill_read_delay_seconds
        self._groups: dict[int, str] = {}
        self._exchange_index: dict[str, int] = {}
        self.halt_check = halt_check
        """Reads the persisted halt flag; set it after building when the store comes later."""
        self.equity_sink = equity_sink
        """Records an equity point (the store's ``record_equity``); set it like ``halt_check``."""

    # -- holdings

    def balance(self) -> Balance:
        """Cash of the subaccount, and equity as cash plus open positions at cost."""
        body = self.client.get("/portfolio/balance", {"subaccount": self.subaccount})
        return parse_balance(body, self.positions())

    def positions(self, event_ticker: str | None = None) -> list[Position]:
        """Non-zero, unsettled positions of the subaccount, optionally for one event."""
        params = {
            "subaccount": self.subaccount,
            "count_filter": "position",
            "event_ticker": event_ticker,
            "limit": PAGE_LIMIT,
        }
        positions = parse_positions(self.client.pages("/portfolio/positions", params))
        if event_ticker is not None:
            positions = [p for p in positions if p.event_ticker == event_ticker]
        return positions

    def execution_book(self, ticker: str) -> OrderBook:
        """The demo exchange's current book for ``ticker``."""
        return self.data.get_order_book(ticker)

    def mark(self, at: datetime | None = None) -> Decimal:
        """The subaccount's marked equity; recorded at ``at`` (else now) through ``equity_sink``.

        Cash plus each open position at its own side's best bid in the demo book,
        or at cost when that side has no bid or the book cannot be read: the same
        definition as ``PaperExecutor.mark``, so the equity series behind the
        stops means one thing in every mode. ``balance().equity`` stays at cost
        (the bankroll sizing uses).
        """
        body = self.client.get("/portfolio/balance", {"subaccount": self.subaccount})
        cash = parse_balance(body, []).cash
        equity = cash
        for position in self.positions():
            bid = self._best_bid(position.market_ticker, position.side)
            equity += position.cost_basis if bid is None else bid * position.contracts
        if self.equity_sink is not None:
            self.equity_sink(at if at is not None else self.clock.now(), equity)
        return equity

    # -- orders

    def place(self, intent: OrderIntent) -> OrderResult:
        """Send one immediate-or-cancel limit order and reconcile its fills."""
        placed_at = self.clock.now()
        halted = self._halted(intent, placed_at)
        if halted is not None:
            return halted
        if intent.contracts < 1:
            return self._rejected(intent, placed_at, "contracts must be at least 1")
        if not (ZERO < intent.limit_price < ONE):
            return self._rejected(intent, placed_at, "limit price must be strictly between 0 and 1")
        try:
            group_id = self._order_group(intent.market_ticker)
        except (KalshiExchangeError, KalshiNotFound) as exc:
            return self._rejected(intent, placed_at, f"order group unavailable: {exc}")
        payload = order_payload(intent, subaccount=self.subaccount, order_group_id=group_id)
        halted = self._halted(intent, placed_at)  # the group lookup took network time
        if halted is not None:
            return halted
        try:
            response = self.client.post(ORDERS_PATH, json=payload)
        except KalshiExchangeError as exc:
            if exc.code == "order_already_exists" or exc.outcome_unknown:
                found = self._find_order(intent)
                if found is not None:
                    return self._from_order(intent, found, placed_at)
                if exc.outcome_unknown:
                    return self._rejected(
                        intent,
                        placed_at,
                        f"outcome unknown and no order with this client_order_id was found: {exc}",
                    )
            return self._rejected(intent, placed_at, str(exc))
        except KalshiNotFound as exc:
            return self._rejected(intent, placed_at, str(exc))
        order_id = str(response.get("order_id") or "")
        if not order_id:
            raise ExchangeError("order response has no order_id")
        filled = count(response.get("fill_count"))
        average_fee = response.get("average_fee_paid")
        placed = utc(response["ts_ms"] / 1000) if response.get("ts_ms") else placed_at
        return self._result(
            intent,
            order_id,
            filled,
            placed_at=placed or placed_at,
            average_fee=decimal(average_fee) if average_fee is not None else None,
        )

    def find_order(self, intent: OrderIntent) -> OrderResult | None:
        """The order already placed with ``intent``'s client order id, if the exchange has one.

        Places nothing. Its fills are reconciled as :meth:`place` reconciles them.
        ``None`` also when the order list cannot be read: the caller then treats
        the outcome as unknown.
        """
        found = self._find_order(intent)
        if found is None:
            return None
        return self._from_order(intent, found, self.clock.now())

    def cancel_all(self) -> int:
        """Cancel every resting order of the subaccount; returns how many were resting.

        ``DELETE /portfolio/events/orders`` answers 204 without a count, so the
        resting orders are listed first. The cancel is sent even when none are
        listed, in case the list lags. Orders here are immediate-or-cancel and
        never rest, so the usual answer is 0.
        """
        params = {"status": "resting", "subaccount": self.subaccount, "limit": PAGE_LIMIT}
        try:
            resting = list(self.client.paginate("/portfolio/orders", params, "orders"))
        except (KalshiExchangeError, KalshiNotFound):
            # Halting must not depend on the count: cancel anyway, then report the failure.
            self.client.delete(ORDERS_PATH, {"subaccount": self.subaccount})
            raise
        self.client.delete(ORDERS_PATH, {"subaccount": self.subaccount})
        return len(resting)

    # -- internals

    def _halted(self, intent: OrderIntent, placed_at: datetime) -> OrderResult | None:
        """A ``halted`` refusal when the persisted halt flag is set, else ``None``."""
        if self.halt_check is None:
            return None
        state = self.halt_check()
        if not state.halted:
            return None
        return self._rejected(intent, placed_at, f"halted: {state.reason}")

    def _best_bid(self, ticker: str, side: Side) -> Decimal | None:
        """The best bid for ``side`` in the demo book now, or ``None`` without one or a book."""
        try:
            book = self.data.get_order_book(ticker)
        except (ExchangeError, NotFound):
            return None
        prices = [lvl.price for lvl in book.bids(side) if lvl.contracts > 0]
        return max(prices) if prices else None

    def _exchange_shard(self, ticker: str) -> int:
        if ticker not in self._exchange_index:
            body = self.client.get(f"/markets/{ticker}")
            self._exchange_index[ticker] = int(
                (body.get("market") or {}).get("exchange_index") or 0
            )
        return self._exchange_index[ticker]

    def _order_group(self, ticker: str) -> str:
        """The subaccount's order group on the market's shard with the configured limit."""
        shard = self._exchange_shard(ticker)
        if shard in self._groups:
            return self._groups[shard]
        limit = self.config.caps.order_group_contracts_limit
        body = self.client.get("/portfolio/order_groups", {"subaccount": self.subaccount})
        group_id = None
        for group in body.get("order_groups") or []:
            raw_limit = group.get("contracts_limit_fp")
            if raw_limit is None or decimal(raw_limit) != Decimal(limit):
                continue
            if int(group.get("exchange_index") or 0) == shard:
                group_id = str(group["id"])
                break
        if group_id is None:
            created = self.client.post(
                "/portfolio/order_groups/create",
                json={
                    "subaccount": self.subaccount,
                    "contracts_limit": limit,
                    "exchange_index": shard,
                },
            )
            group_id = str(created["order_group_id"])
        self._groups[shard] = group_id
        return group_id

    def _find_order(self, intent: OrderIntent) -> Mapping[str, Any] | None:
        params = {
            "ticker": intent.market_ticker,
            "subaccount": self.subaccount,
            "limit": PAGE_LIMIT,
        }
        try:
            for order in self.client.paginate("/portfolio/orders", params, "orders"):
                if order.get("client_order_id") == intent.client_order_id:
                    return order
        except (KalshiExchangeError, KalshiNotFound):
            return None
        return None

    def _from_order(
        self, intent: OrderIntent, order: Mapping[str, Any], placed_at: datetime
    ) -> OrderResult:
        filled = count(order.get("fill_count_fp"))
        fees = count(order.get("taker_fees_dollars")) + count(order.get("maker_fees_dollars"))
        average_fee = fees / filled if filled > 0 else None
        created = utc(order.get("created_time")) or placed_at
        return self._result(
            intent, str(order["order_id"]), filled, placed_at=created, average_fee=average_fee
        )

    def _result(
        self,
        intent: OrderIntent,
        order_id: str,
        filled: Decimal,
        *,
        placed_at: datetime,
        average_fee: Decimal | None,
    ) -> OrderResult:
        fills: list[Fill] = []
        error = ""
        read_failed = ""
        if filled > 0:
            try:
                fills = self._reconciled_fills(order_id, filled)
            except (KalshiExchangeError, KalshiNotFound) as exc:
                # The order was accepted: it must come back as a result, not an exception,
                # so the caller stores it. The fills are recorded at the limit price below.
                fills, read_failed = [], f"fills could not be read ({exc}); "
            recorded = sum((f.contracts for f in fills), ZERO)
            if recorded < filled:
                shortfall = filled - recorded
                fills.append(
                    Fill(
                        fill_id=f"{order_id}:unreconciled",
                        order_id=order_id,
                        market_ticker=intent.market_ticker,
                        side=intent.side,
                        contracts=shortfall,
                        price=intent.limit_price,
                        fee=(average_fee or ZERO) * shortfall,
                        filled_at=placed_at,
                        simulated=False,
                    )
                )
                error = (
                    f"{read_failed}fills not yet reconciled: {shortfall} of {filled} contracts "
                    "recorded at the limit price"
                )
        if filled <= 0:
            status = OrderStatus.UNFILLED
        elif filled >= intent.contracts:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.PARTIALLY_FILLED
        return OrderResult(
            order_id=order_id,
            client_order_id=intent.client_order_id,
            episode_id=intent.episode_id,
            market_ticker=intent.market_ticker,
            side=intent.side,
            requested_contracts=intent.contracts,
            limit_price=intent.limit_price,
            status=status,
            fills=fills,
            error=error,
            placed_at=placed_at,
        )

    def _reconciled_fills(self, order_id: str, expected: Decimal) -> list[Fill]:
        """The order's fills from ``GET /portfolio/fills``, re-read briefly while they lag ``expected``."""
        params = {"order_id": order_id, "subaccount": self.subaccount, "limit": PAGE_LIMIT}
        fills: list[Fill] = []
        for attempt in range(self._fill_read_attempts):
            fills = [
                parse_fill(raw)
                for raw in self.client.paginate("/portfolio/fills", params, "fills")
                if raw.get("order_id") == order_id
            ]
            recorded = sum((f.contracts for f in fills), ZERO)
            if recorded >= expected or attempt == self._fill_read_attempts - 1:
                break
            self._sleep(self._fill_read_delay)
        return sorted(fills, key=lambda f: (f.filled_at, f.fill_id))

    def _rejected(self, intent: OrderIntent, placed_at: datetime, error: str) -> OrderResult:
        return OrderResult(
            order_id=stable_id("demo-rejected", intent.client_order_id),
            client_order_id=intent.client_order_id,
            episode_id=intent.episode_id,
            market_ticker=intent.market_ticker,
            side=intent.side,
            requested_contracts=intent.contracts,
            limit_price=intent.limit_price,
            status=OrderStatus.REJECTED,
            error=error,
            placed_at=placed_at,
        )


def _demo_client(
    config: Config,
    *,
    signer: Any,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> KalshiClient:
    base_url = config.kalshi.demo_base_url
    if not is_demo_url(base_url):
        raise ConfigError(f"kalshi.demo_base_url is not a demo host: {base_url!r}")
    return KalshiClient(
        base_url,
        signer=signer,
        max_requests_per_second=config.kalshi.max_requests_per_second,
        timeout=config.kalshi.request_timeout_seconds,
        transport=transport,
        user_agent=config.sources.user_agent,
        sleep=sleep,
        monotonic=monotonic,
    )


def demo_exchange(
    config: Config,
    clock: Clock,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> KalshiData:
    """Market data from the demo host (public endpoints, unsigned).

    Kalshi warns that demo prices may not reflect real markets; demo mode
    exercises execution plumbing, and its P&L is never reported as evidence.
    """
    client = _demo_client(
        config, signer=None, transport=transport, sleep=sleep, monotonic=monotonic
    )
    return KalshiData(client, clock, config)


def no_credentials_message(config: Config) -> str:
    """What :func:`demo_executor` says when no demo credential is set at all (names, never values)."""
    cfg = config.kalshi
    return (
        f"demo mode needs {cfg.demo_key_id_env} and {cfg.demo_private_key_env} "
        f"or {cfg.demo_private_key_path_env}"
    )


def demo_executor(
    config: Config,
    clock: Clock,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    halt_check: Callable[[], HaltState] | None = None,
) -> DemoExecutor:
    """A :class:`DemoExecutor` signed with the demo key from the environment.

    ``halt_check`` (the state store's ``get_halt``) is read before every order is
    sent; it can also be set on the executor afterwards (``executor.halt_check``).
    Raises :class:`ConfigError` when no demo credentials are set.
    """
    signer = load_signer(config, environ=environ)
    if signer is None:
        raise ConfigError(no_credentials_message(config))
    client = _demo_client(
        config, signer=signer, transport=transport, sleep=sleep, monotonic=monotonic
    )
    data = demo_exchange(config, clock, transport=transport, sleep=sleep, monotonic=monotonic)
    return DemoExecutor(client, clock, config, data=data, sleep=sleep, halt_check=halt_check)


__all__ = [
    "DEMO_HOSTS",
    "DemoExecutor",
    "demo_exchange",
    "demo_executor",
    "is_demo_url",
    "no_credentials_message",
    "order_payload",
]
