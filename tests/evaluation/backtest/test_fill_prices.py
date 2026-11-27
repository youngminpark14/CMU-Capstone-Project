"""What a backtest decides with and what it fills against.

The decision (the execute re-read, the one recompute, the gate, routing's price
move) reads ``Executor.execution_book``, which must show only prices at or before
the as-of time. The fill alone is priced from the first one-minute candle after
the as-of time plus the latency, and is timed at that candle's end, so the
account read at the as-of time does not hold it.

The cut market gets one-minute candles here: an as-of candle at the clock's time
asking 0.31, and the next candle (the fill candle) asking 0.35.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pmfa.config import Config
from pmfa.records.decision import OrderIntent, OrderStatus
from pmfa.records.market import OHLC, Candle, Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.backtest import BacktestData, build_backtest_world
from pmfa.world.clocks import SimClock
from pmfa.world.snapshot.store import SnapshotStore, snapshot_path
from tests.evaluation.backtest.support import FED, scripted

AS_OF = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
CUT = f"{FED}-C25"


def _flat(value: str) -> OHLC:
    v = Decimal(value)
    return OHLC(open=v, high=v, low=v, close=v)


def _minute(end: datetime, ask: str) -> Candle:
    return Candle(
        ticker=CUT,
        end_ts=end,
        interval_minutes=1,
        yes_bid=_flat("0.29"),
        yes_ask=_flat(ask),
        price=_flat("0.30"),
        volume=Decimal("500"),
        open_interest=Decimal("1000"),
    )


def _world(config: Config) -> tuple[object, SqliteStore, SimClock, BacktestData]:
    snapshot = SnapshotStore(snapshot_path(config))
    start = AS_OF - timedelta(hours=2)
    snapshot.put_candles(
        CUT,
        1,
        [_minute(AS_OF, "0.31"), _minute(AS_OF + timedelta(minutes=2), "0.35")],
        start=start,
        end=AS_OF + timedelta(hours=1),
    )
    snapshot.close()
    data = BacktestData(config)
    clock = SimClock(AS_OF)
    store = SqliteStore(MEMORY)
    world = build_backtest_world(
        data, config, clock=clock, store=store, models=scripted(config), knowledge=object()
    )
    return world, store, clock, data


def _intent(limit: str, coid: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=coid,
        episode_id="ep-1",
        proposal_id="prop-1",
        market_ticker=CUT,
        side=Side.YES,
        contracts=5,
        limit_price=Decimal(limit),
    )


def test_the_execution_book_is_the_as_of_book(data_config: Config) -> None:
    world, _, clock, data = _world(data_config)
    book = world.executor.execution_book(CUT)  # type: ignore[attr-defined]
    assert book == world.exchange.get_order_book(CUT)  # type: ignore[attr-defined]
    assert book.as_of <= clock.now()
    assert book.best_ask(Side.YES).price == Decimal("0.31")  # not the later candle's 0.35
    data.close()


def test_the_fill_is_priced_from_the_candle_after_the_as_of_time(data_config: Config) -> None:
    world, store, _, data = _world(data_config)
    executor = world.executor  # type: ignore[attr-defined]
    # The as-of ask is 0.31, but the fill candle asks 0.35: an order at 0.31 is unfilled.
    assert executor.place(_intent("0.31", "c1")).status is OrderStatus.UNFILLED
    filled = executor.place(_intent("0.35", "c2"))
    assert [(f.contracts, f.price) for f in filled.fills] == [(5, Decimal("0.35"))]
    granularity = data.execution_book(CUT, filled.placed_at).granularity
    assert granularity == "minute"
    assert len(store.list_orders()) == 2
    data.close()


def test_the_fill_is_timed_at_the_end_of_its_candle_and_not_seen_before_then(
    data_config: Config,
) -> None:
    world, store, clock, data = _world(data_config)
    executor = world.executor  # type: ignore[attr-defined]
    filled = executor.place(_intent("0.35", "c1"))
    candle_end = AS_OF + timedelta(minutes=2)
    assert filled.placed_at == AS_OF
    assert filled.outcome_at == candle_end
    assert [f.filled_at for f in filled.fills] == [candle_end]
    assert data.execution_book(CUT, AS_OF).known_at == candle_end
    # Read at the as-of time, as a second episode at the same instant would read it.
    assert executor.positions() == []
    assert executor.account(as_of=AS_OF).holdings == []
    assert [t for t, _ in store.equity_series()] == [candle_end]  # no point at the as-of time
    clock.set(candle_end)
    assert [(p.market_ticker, p.contracts) for p in executor.positions()] == [(CUT, 5)]
    data.close()


def test_the_printed_fill_assumptions_keep_the_wording_the_tests_above_check() -> None:
    """Pins the wording only: the behaviour behind each sentence is checked by the tests above
    and by tests/evaluation/backtest/test_trading_pass.py."""
    from pmfa.evaluation.manifest import FILL_ASSUMPTIONS, LEAKAGE_CONTROLS

    fills = " ".join(FILL_ASSUMPTIONS)
    assert "every decision" in fills and "reads the order book as of the as-of time" in fills
    assert "Only the fill is priced from the first one-minute candle after the as-of time" in fills
    assert "flagged coarse" in fills and "execution.backtest_volume_share" in fills
    assert "is under one contract" in fills and "no quote to price it from" in fills
    assert "A fill is timed at the end of the candle that priced it" in fills
    assert "count an order only from that time" in fills
    assert "an episode can start later than its trigger" in fills
    assert "The scoring pass keeps its fixed as-of times" in fills
    leakage = " ".join(LEAKAGE_CONTROLS)
    assert "only the paper fill is priced from a later candle" in leakage
    assert "A paper fill is timed at the end of the candle that priced it" in leakage
    assert "the next episode starts no earlier than the latest order's outcome time" in leakage


def test_the_report_does_not_say_how_fills_were_priced_beyond_what_it_counted(
    data_config: Config, tmp_path
) -> None:
    from pmfa.evaluation.backtest import run_backtest
    from pmfa.evaluation.report import build_report

    run_dir = tmp_path / "run"
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    text = build_report(run_dir).read_text()
    priced = text.split("## How each fill was priced", 1)[1].split("\n## ", 1)[0]
    # A stale_price exit is decided on the as-of book; the fill book never reaches it.
    assert "the as-of book offered nothing at or below the limit" in priced
    limitations = text.split("## Limitations", 1)[1].split("\n## ", 1)[0]
    assert "many would use the hourly fallback" not in limitations
    assert "How each fill was priced" in limitations
