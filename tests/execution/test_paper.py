"""The paper executor: walking the book, immediate-or-cancel, idempotent ids, the execution book.

The hold market's YES offers are 0.62 x 30 and 0.64 x 40 at the trigger.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmfa.decision import series_fee
from pmfa.ports import Executor
from pmfa.records.decision import OrderStatus
from pmfa.records.market import Side
from tests.world.recorded.factories import AS_OF, EVENT, HOLD, book, series, world_file

from .factories import intent, paper


def fee(n: int, price: str) -> Decimal:
    return series_fee(n, Decimal(price), series())


def test_paper_executor_is_a_simulated_executor() -> None:
    executor, _, _ = paper()
    assert isinstance(executor, Executor)
    assert executor.simulated is True
    assert executor.cancel_all() == 0


def test_full_fill_at_the_best_level() -> None:
    executor, store, _ = paper()
    result = executor.place(intent(10, "0.62"))
    assert result.status is OrderStatus.FILLED
    [f] = result.fills
    assert (f.contracts, f.price, f.fee, f.simulated) == (
        10,
        Decimal("0.62"),
        Decimal("0.17"),
        True,
    )
    assert f.fee == fee(10, "0.62")
    assert store.list_orders() == [result]
    [position] = executor.positions()
    assert (position.event_ticker, position.side, position.contracts) == (EVENT, Side.YES, 10)
    assert executor.balance().cash == Decimal("1000") - Decimal("6.20") - Decimal("0.17")
    assert store.equity_series() == [(AS_OF, Decimal("999.83"))]


def test_walks_two_levels_with_one_fill_and_one_fee_per_level() -> None:
    executor, _, _ = paper()
    result = executor.place(intent(50, "0.64"))
    assert result.status is OrderStatus.FILLED
    assert [(f.contracts, f.price, f.fee) for f in result.fills] == [
        (30, Decimal("0.62"), fee(30, "0.62")),
        (20, Decimal("0.64"), fee(20, "0.64")),
    ]
    assert result.fees == Decimal("0.50") + Decimal("0.33")
    [position] = executor.positions(EVENT)
    assert position.contracts == 50
    assert position.avg_price == Decimal("0.6280")


def test_partial_fill_records_the_filled_part_and_drops_the_rest() -> None:
    executor, store, _ = paper()
    result = executor.place(intent(100, "0.64"))
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert result.requested_contracts == 100
    assert result.filled_contracts == 70
    assert executor.positions()[0].contracts == 70
    assert executor.cancel_all() == 0  # nothing rests
    assert len(store.list_orders()) == 1


def test_nothing_at_or_below_the_limit_is_unfilled() -> None:
    executor, store, _ = paper()
    result = executor.place(intent(10, "0.61"))
    assert result.status is OrderStatus.UNFILLED
    assert result.fills == []
    assert executor.positions() == []
    assert store.list_orders() == [result]
    assert executor.balance().cash == Decimal("1000")


def test_no_side_walks_the_yes_bids() -> None:
    executor, _, _ = paper()
    result = executor.place(intent(5, "0.40", side=Side.NO))
    assert [(f.side, f.contracts, f.price) for f in result.fills] == [(Side.NO, 5, Decimal("0.40"))]


def test_a_retry_with_the_same_client_order_id_returns_the_first_order() -> None:
    executor, store, clock = paper()
    first = executor.place(intent(10, "0.64"))
    clock.advance(30)
    again = executor.place(intent(10, "0.64"))
    assert again == first
    assert len(store.list_orders()) == 1
    assert len(store.list_fills()) == 1
    other = executor.place(intent(10, "0.64", client_order_id="coid-2"))
    assert other.order_id != first.order_id
    assert len(store.list_orders()) == 2


def test_the_execution_book_can_differ_from_the_trigger_book() -> None:
    moved = world_file(execution_books={HOLD: book(no_bids=(("0.35", "100"),))})
    executor, _, _ = paper(moved)
    assert executor.execution_book(HOLD).best_ask(Side.YES).price == Decimal("0.65")
    assert executor.place(intent(10, "0.64")).status is OrderStatus.UNFILLED
    filled = executor.place(intent(10, "0.65", client_order_id="coid-2"))
    assert [(f.contracts, f.price) for f in filled.fills] == [(10, Decimal("0.65"))]


def test_halted_orders_are_refused_and_not_stored() -> None:
    executor, store, _ = paper()
    store.set_halt("manual", AS_OF)
    result = executor.place(intent(10, "0.62"))
    assert result.status is OrderStatus.REJECTED
    assert store.list_orders() == []
    store.clear_halt()
    assert executor.place(intent(10, "0.62")).status is OrderStatus.FILLED


def test_settle_records_the_settlement_and_an_equity_point() -> None:
    executor, store, _ = paper(bankroll="500")
    executor.place(intent(10, "0.62"))
    later = AS_OF + timedelta(days=19)
    executor.settle(HOLD, Side.YES, later)
    assert store.list_settlements() == [(HOLD, Side.YES, later)]
    balance = executor.balance()
    assert balance.cash == balance.equity == Decimal("500") - Decimal("6.37") + Decimal("10")
    assert store.equity_series()[-1] == (later, balance.equity)
    assert executor.positions() == []
