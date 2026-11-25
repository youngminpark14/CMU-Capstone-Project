"""The execution book: first minute candle after as-of plus latency, conservative side, capped."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmfa.config import Config
from pmfa.records.market import Side
from pmfa.world.snapshot.book import execution_book
from pmfa.world.snapshot.store import SnapshotStore
from tests.world.snapshot.fakes import JUL_CLOSE, T0, A

S = timedelta(seconds=1)
M = timedelta(minutes=1)


def test_uses_the_minute_candle_after_as_of_plus_latency(
    store: SnapshotStore, config: Config
) -> None:
    # Sent 11:59:30; arrives 12:00:30 (60 s latency); the 12:01 candle contains that moment.
    result = execution_book(store, A, T0 - 30 * S, config)
    assert result.arrival == T0 + 30 * S
    assert result.granularity == "minute" and not result.coarse
    assert result.candle is not None and result.candle.end_ts == T0 + M
    # Without latency, the order would have met the 12:00 hourly period instead.
    no_latency = config.model_copy(
        update={"execution": config.execution.model_copy(update={"backtest_latency_seconds": 0})}
    )
    assert execution_book(store, A, T0 - 30 * S, no_latency).granularity == "hour"


def test_prices_at_the_conservative_side_of_the_minute(
    store: SnapshotStore, config: Config
) -> None:
    book = execution_book(store, A, T0 - 30 * S, config).book
    # 12:01 candle: YES ask high 0.46, YES bid low 0.39, volume 100 -> 10 contracts.
    assert book.best_ask(Side.YES).price == Decimal("0.46")
    assert book.best_ask(Side.NO).price == Decimal("0.61")
    assert book.best_ask(Side.YES).contracts == 10
    assert book.best_ask(Side.NO).contracts == 10


def test_size_is_capped_at_the_volume_share_and_floored(
    store: SnapshotStore, config: Config
) -> None:
    # 12:02 candle: volume 50 -> 5 contracts.
    assert execution_book(store, A, T0 + 30 * S, config).book.best_ask(Side.YES).contracts == 5
    # 12:40 candle: volume 7 -> 0.7 -> 0: an empty book, but the candle is reported.
    thin = execution_book(store, A, T0 + 38 * M + 30 * S, config)
    assert thin.candle is not None and thin.candle.end_ts == T0 + 40 * M
    assert thin.book.yes_bids == [] and thin.book.no_bids == []
    half = config.model_copy(
        update={
            "execution": config.execution.model_copy(
                update={"backtest_volume_share": Decimal("0.5")}
            )
        }
    )
    assert execution_book(store, A, T0 + 30 * S, half).book.best_ask(Side.YES).contracts == 25


def test_falls_back_to_the_hourly_candle_and_says_so(store: SnapshotStore, config: Config) -> None:
    # Arrives 12:11: no minute candle in (12:11, 12:12]; the 13:00 hourly candle contains it.
    result = execution_book(store, A, T0 + 10 * M, config)
    assert result.granularity == "hour" and result.coarse
    assert result.candle is not None and result.candle.end_ts == T0 + timedelta(hours=1)
    # Hourly: YES ask high 0.52, YES bid low 0.39, volume 187 -> 18 contracts.
    assert result.book.best_ask(Side.YES).price == Decimal("0.52")
    assert result.book.best_ask(Side.NO).price == Decimal("0.61")
    assert result.book.best_ask(Side.YES).contracts == 18


def test_empty_when_no_candle_or_after_close(store: SnapshotStore, config: Config) -> None:
    nothing = execution_book(store, A, T0 + timedelta(hours=5), config)
    assert nothing.granularity == "none" and nothing.candle is None
    assert nothing.book.yes_bids == [] and nothing.book.no_bids == []
    closed = execution_book(store, A, JUL_CLOSE - 30 * S, config)
    assert closed.granularity == "none"
    assert closed.book.yes_bids == [] and closed.book.no_bids == []
