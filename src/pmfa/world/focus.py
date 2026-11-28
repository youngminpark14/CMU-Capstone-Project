"""A world narrowed to one market: the event an operator asked about, with only that market.

``pmfa ask`` forecasts one market, not a whole event. The Bitcoin series lists
close to two hundred strikes per hourly event; narrowed to one, the existing
ladder outcome space has two outcomes, the market's YES condition and its
complement, and the episode sizes, gates and fills that market alone.

:class:`FocusedExchange` wraps any ``Exchange``: ``get_event`` and
``list_events`` return the chosen event with only the chosen market, and every
other call is passed through unchanged. :func:`focus_world` puts it behind the
world's exchange and rebuilds the paper executor on it, so every pipeline step
and the executor read through it. Nothing is written or cached here.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal

from pmfa.errors import NotFound
from pmfa.ports import Exchange, World
from pmfa.records.market import Candle, Event, Market, OrderBook, Series, Side


class FocusedExchange:
    """The ``Exchange`` port with one event narrowed to one of its markets."""

    def __init__(self, inner: Exchange, event_ticker: str, market_ticker: str) -> None:
        self.inner = inner
        self.event_ticker = event_ticker
        self.market_ticker = market_ticker

    def _narrow(self, event: Event) -> Event:
        if event.event_ticker != self.event_ticker:
            return event
        markets = [m for m in event.markets if m.ticker == self.market_ticker]
        if not markets:
            raise NotFound(f"event {self.event_ticker} no longer lists {self.market_ticker}")
        return event.model_copy(update={"markets": markets})

    def get_event(self, event_ticker: str) -> Event:
        return self._narrow(self.inner.get_event(event_ticker))

    def list_events(self, series_ticker: str, *, include_closed: bool = False) -> list[Event]:
        events = self.inner.list_events(series_ticker, include_closed=include_closed)
        return [self._narrow(e) for e in events]

    def get_series(self, series_ticker: str) -> Series:
        return self.inner.get_series(series_ticker)

    def get_market(self, ticker: str) -> Market:
        return self.inner.get_market(ticker)

    def get_order_book(self, ticker: str) -> OrderBook:
        return self.inner.get_order_book(ticker)

    def get_candles(
        self, ticker: str, *, start: datetime, end: datetime, interval_minutes: int
    ) -> list[Candle]:
        return self.inner.get_candles(
            ticker, start=start, end=end, interval_minutes=interval_minutes
        )

    def liquidity_dollars(self, ticker: str, side: Side) -> Decimal:
        return self.inner.liquidity_dollars(ticker, side)


def focus_world(world: World, event_ticker: str, market_ticker: str) -> World:
    """``world`` with its exchange focused on one market, and a paper executor that reads it.

    The paper executor is rebuilt over the same store, clock, books and bankroll,
    so the account is the same one; any other executor is kept as it is.
    """
    from pmfa.execution.paper import PaperExecutor

    exchange = FocusedExchange(world.exchange, event_ticker, market_ticker)
    executor = world.executor
    if isinstance(executor, PaperExecutor):
        executor = PaperExecutor(
            executor.store,
            executor.clock,
            executor.config,
            exchange,
            executor.books,
            bankroll=executor.bankroll,
            fill_books=executor.fill_books,
        )
    return dataclasses.replace(world, exchange=exchange, executor=executor)


__all__ = ["FocusedExchange", "focus_world"]
