"""The paper executor: walking the book, immediate-or-cancel, idempotent ids, the execution book.

The hold market's YES offers are 0.62 x 30 and 0.64 x 40 at the trigger.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmfa.decision import series_fee
from pmfa.execution.paper import PaperExecutor
from pmfa.ports import Executor
from pmfa.records.decision import OrderIntent, OrderStatus
from pmfa.records.market import OrderBook, Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.clocks import SimClock
from pmfa.world.recorded import RecordedExchange
from tests.world.recorded.factories import AS_OF, CUT, EVENT, HOLD, book, series, world_file

from .factories import CONFIG, intent, paper


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
    assert executor.balance().equity == Decimal("999.83")  # holdings at cost: sizing's bankroll
    # The equity point for the stops marks the 10 YES at the 0.60 YES bid.
    assert store.equity_series() == [(AS_OF, Decimal("993.63") + Decimal("6.00"))]


def test_walks_two_levels_and_rounds_the_fee_up_once_for_the_order() -> None:
    executor, _, _ = paper()
    result = executor.place(intent(50, "0.64"))
    assert result.status is OrderStatus.FILLED
    assert [(f.contracts, f.price) for f in result.fills] == [
        (30, Decimal("0.62")),
        (20, Decimal("0.64")),
    ]
    # 30 x 0.07 x 0.62 x 0.38 = 0.49476 and 20 x 0.07 x 0.64 x 0.36 = 0.32256, so 0.81732 in
    # all: charged as 0.82 for the order. Rounding each level up would have charged 0.83.
    assert result.fees == Decimal("0.82")
    assert [f.fee for f in result.fills] == [Decimal("0.4947"), Decimal("0.3253")]


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


# -- review fixes: the decision book and the fill book, marks, settlement times, depth


def _executor_on(
    books: dict[str, OrderBook], *, fill_books: dict[str, OrderBook] | None = None
) -> tuple[PaperExecutor, SqliteStore, SimClock]:
    file = world_file()
    clock = SimClock(AS_OF)
    store = SqliteStore(MEMORY)
    executor = PaperExecutor(
        store,
        clock,
        CONFIG,
        RecordedExchange(file, clock, CONFIG),
        lambda t: books[t],
        fill_books=(lambda t: fill_books[t]) if fill_books is not None else None,
    )
    return executor, store, clock


def test_the_decision_book_and_the_fill_book_can_be_separate() -> None:
    as_of = book()  # YES offers 0.62 x 30, 0.64 x 40
    later = book(no_bids=(("0.30", "100"),))  # YES offer 0.70 x 100
    executor, _, _ = _executor_on({HOLD: as_of}, fill_books={HOLD: later})
    assert executor.execution_book(HOLD) == as_of  # what the pipeline decides with
    unfilled = executor.place(intent(10, "0.64"))
    assert unfilled.status is OrderStatus.UNFILLED  # the later book does not meet the limit
    filled = executor.place(intent(10, "0.70", client_order_id="coid-2"))
    assert [(f.contracts, f.price) for f in filled.fills] == [(10, Decimal("0.70"))]


def test_without_a_fill_book_orders_fill_against_the_execution_book() -> None:
    executor, _, _ = _executor_on({HOLD: book()})
    assert executor.place(intent(10, "0.62")).status is OrderStatus.FILLED


def test_equity_marks_value_holdings_at_their_own_bid() -> None:
    books = {HOLD: book()}  # YES bid 0.60
    executor, store, clock = _executor_on(books)
    executor.place(intent(10, "0.62"))  # cost 6.20, fee 0.17
    cash = Decimal("1000") - Decimal("6.20") - Decimal("0.17")
    # Sizing's bankroll stays at cost; the stops' equity point is at the YES bid.
    assert executor.balance().equity == cash + Decimal("6.20")
    assert store.equity_series() == [(AS_OF, cash + Decimal("6.00"))]
    # The market falls to a 0.02 bid: the mark shows the unrealized loss.
    books[HOLD] = book(yes_bids=(("0.02", "50"),))
    clock.advance(60)
    assert executor.mark() == cash + Decimal("0.20")
    # No bid at all: the holding is carried at cost.
    books[HOLD] = book(yes_bids=())
    clock.advance(60)
    assert executor.mark() == cash + Decimal("6.20")
    assert executor.balance().equity == cash + Decimal("6.20")


def test_a_no_holding_is_marked_at_the_no_bid() -> None:
    books = {HOLD: book()}  # NO bids 0.38 x 30, 0.36 x 40; YES ask 0.40 from the 0.60 YES bid
    executor, _, _ = _executor_on(books)
    executor.place(intent(5, "0.40", side=Side.NO))
    acct = executor.account()
    assert executor.mark() == acct.cash + 5 * Decimal("0.38")


def test_the_drawdown_halt_sees_an_unrealized_loss() -> None:
    from pmfa.decision.risk import risk_state

    books = {HOLD: book(no_bids=(("0.38", "1000"),))}
    executor, store, clock = _executor_on(books)
    executor.mark(AS_OF - timedelta(days=1))
    executor.place(intent(500, "0.62"))  # $310 at cost
    books[HOLD] = book(yes_bids=(("0.30", "50"),))
    clock.advance(60)
    executor.mark()
    risk = risk_state(
        store.equity_series(), clock.now(), CONFIG, bankroll=executor.balance().equity
    )
    assert risk.drawdown_tripped and risk.loss_stop_tripped


def test_a_settlement_seen_late_is_marked_at_the_current_time() -> None:
    """Settlements processed out of time order must not leave a stale latest equity point."""
    from pmfa.decision.risk import risk_state

    executor, store, clock = paper()
    executor.place(intent(10, "0.62"))
    executor.place(intent(5, "0.40", side=Side.NO, client_order_id="coid-2"))
    executor.place(
        OrderIntent(
            client_order_id="coid-3",
            episode_id="ep-1",
            proposal_id="prop-1",
            market_ticker=CUT,
            side=Side.YES,
            contracts=10,
            limit_price=Decimal("0.99"),
        )
    )
    clock.set(AS_OF + timedelta(days=2))
    # The resolver walks tickers in order: the later settlement (CUT) comes first.
    executor.settle(CUT, Side.NO, AS_OF + timedelta(days=1, hours=1))
    executor.settle(HOLD, Side.NO, AS_OF + timedelta(days=1))
    true_equity = executor.balance().equity
    series = store.equity_series()
    assert max(series)[0] == clock.now()
    assert max(series)[1] == true_equity
    risk = risk_state(series, clock.now(), CONFIG, bankroll=true_equity)
    peak = max(e for _, e in series)
    assert risk.drawdown_fraction == (peak - true_equity) / peak


def test_a_fractional_level_offers_only_its_whole_contracts() -> None:
    # YES asks 0.50 x 10.5 and 0.51 x 10.5: each level offers floor(10.5) = 10 whole contracts,
    # the same rule sizing uses for displayed depth.
    fractional = book(no_bids=(("0.50", "10.5"), ("0.49", "10.5")))
    executor, _, _ = _executor_on({HOLD: fractional})
    result = executor.place(intent(20, "0.51"))
    assert result.status is OrderStatus.FILLED
    assert [(f.contracts, f.price) for f in result.fills] == [
        (10, Decimal("0.50")),
        (10, Decimal("0.51")),
    ]
