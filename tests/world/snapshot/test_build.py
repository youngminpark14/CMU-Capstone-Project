"""The builder against the invented exchange: what is fetched, over which ranges, and resuming."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.records.market import Side
from pmfa.world.snapshot.build import build_snapshot
from pmfa.world.snapshot.store import SnapshotStore
from tests.world.snapshot.fakes import (
    AUG,
    AUG_CLOSE,
    BUILT,
    CPI,
    FED,
    JUL,
    JUL_CLOSE,
    A,
    B,
    C,
    D,
    FakeExchange,
    Interrupted,
    candles,
    utc,
)

WINDOW = utc(2026, 7, 1)


def _all_candles(store: SnapshotStore) -> dict[tuple[str, int], list]:
    lo, hi = utc(2026, 1, 1), utc(2027, 1, 1)
    return {(t, i): store.candles(t, i, after=lo, until=hi) for t in (A, B, C, D) for i in (1, 60)}


def test_build_stores_events_markets_results_and_manifest(store: SnapshotStore) -> None:
    assert store.series_tickers() == [CPI, FED]
    assert store.event_tickers(FED) == [JUL, AUG]
    jul = store.get_event(JUL)
    assert [m.ticker for m in jul.markets] == sorted([A, B])
    assert store.get_market(A).result is Side.YES
    assert store.get_market(A).rules_primary.startswith("Resolves YES")
    manifest = store.manifest()
    assert manifest is not None
    assert manifest.series == [FED, CPI]
    assert manifest.window_start == WINDOW
    assert manifest.window_end == BUILT
    assert manifest.historical_cutoff == utc(2026, 9, 26)
    assert manifest.counts == {"events": 2, "markets": 4, "candles_1m": 7, "candles_60m": 5}
    assert manifest.minute_markets == 4


def test_candles_round_trip_exactly(store: SnapshotStore) -> None:
    for (ticker, interval), expected in candles().items():
        assert _all_candles(store)[(ticker, interval)] == expected


def test_ranges_follow_open_time_window_start_and_close(config: Config, tmp_path: Path) -> None:
    exchange = FakeExchange()
    build_snapshot(config, exchange, SnapshotStore(tmp_path / "s.sqlite"), end=BUILT)
    ranges = {(t, i): (lo, hi) for t, i, lo, hi in exchange.candle_calls}
    # A opened 2026-06-15, after window start minus 30 days: hourly from its open.
    assert ranges[(A, 60)] == (utc(2026, 6, 15, 14), JUL_CLOSE)
    # Minute candles start at the window start.
    assert ranges[(A, 1)] == (WINDOW, JUL_CLOSE)
    # D opened after the window start: both intervals from its open.
    assert ranges[(D, 60)] == (utc(2026, 8, 1), AUG_CLOSE)
    assert ranges[(D, 1)] == (utc(2026, 8, 1), AUG_CLOSE)
    assert exchange.event_calls == [(FED, WINDOW, BUILT), (CPI, WINDOW, BUILT)]


def test_hourly_reaches_thirty_days_before_the_window(config: Config, tmp_path: Path) -> None:
    exchange = FakeExchange()
    start = utc(2026, 7, 20)  # minus 30 days is 2026-06-20, after A opened
    build_snapshot(config, exchange, SnapshotStore(tmp_path / "s.sqlite"), start=start, end=BUILT)
    ranges = {(t, i): (lo, hi) for t, i, lo, hi in exchange.candle_calls}
    assert ranges[(A, 60)][0] == start - timedelta(days=30)
    assert ranges[(A, 1)][0] == start


def test_minute_days_limits_one_minute_range(config: Config, tmp_path: Path) -> None:
    exchange = FakeExchange()
    store = SnapshotStore(tmp_path / "s.sqlite")
    manifest = build_snapshot(config, exchange, store, end=BUILT, minute_days=5)
    ranges = {(t, i): (lo, hi) for t, i, lo, hi in exchange.candle_calls}
    assert ranges[(A, 1)] == (JUL_CLOSE - timedelta(days=5), JUL_CLOSE)
    assert ranges[(D, 1)] == (AUG_CLOSE - timedelta(days=5), AUG_CLOSE)
    assert ranges[(A, 60)] == (utc(2026, 6, 15, 14), JUL_CLOSE)  # hourly unchanged
    assert manifest.minute_days == 5
    # The 2026-07-20 minute candles fall outside the last five days and are not stored.
    assert store.candles(A, 1, after=WINDOW, until=JUL_CLOSE) == []


def test_events_closing_outside_the_window_are_left_out(config: Config, tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "s.sqlite")
    build_snapshot(config, FakeExchange(), store, end=utc(2026, 8, 1))
    assert store.event_tickers() == [JUL]


def test_only_enabled_series_are_built(tmp_path: Path) -> None:
    config = Config()
    series = [s.model_copy(update={"enabled": s.ticker == FED}) for s in config.follow.series]
    config = config.model_copy(
        update={"follow": config.follow.model_copy(update={"series": series})}
    )
    exchange = FakeExchange()
    store = SnapshotStore(tmp_path / "s.sqlite")
    build_snapshot(config, exchange, store, end=BUILT)
    assert [c[0] for c in exchange.event_calls] == [FED]
    assert store.series_tickers() == [FED]


def test_interrupted_build_resumes_where_it_stopped(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    first = FakeExchange(fail_after=3)
    with pytest.raises(Interrupted):
        build_snapshot(config, first, SnapshotStore(path), end=BUILT)
    done = {(t, i) for t, i, _, _ in first.candle_calls}
    assert len(done) == 3

    interrupted = SnapshotStore(path)
    assert interrupted.manifest() is None  # the manifest is written only at the end

    second = FakeExchange()
    progress: list[str] = []
    manifest = build_snapshot(config, second, interrupted, end=BUILT, progress=progress.append)
    redone = {(t, i) for t, i, _, _ in second.candle_calls}
    assert redone.isdisjoint(done)
    assert done | redone == {(t, i) for t in (A, B, C, D) for i in (1, 60)}
    assert sum("already complete" in line for line in progress) == 3

    clean = SnapshotStore(tmp_path / "clean.sqlite")
    build_snapshot(config, FakeExchange(), clean, end=BUILT)
    assert _all_candles(interrupted) == _all_candles(clean)
    assert manifest.counts == clean.counts()


def test_a_wider_range_is_fetched_again(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    build_snapshot(config, FakeExchange(), SnapshotStore(path), end=BUILT, minute_days=1)
    again = FakeExchange()
    store = SnapshotStore(path)
    build_snapshot(config, again, store, end=BUILT)
    assert {(t, i) for t, i, _, _ in again.candle_calls} == {(t, 1) for t in (A, B, C, D)}
    assert len(store.candles(A, 1, after=WINDOW, until=JUL_CLOSE)) == 4


def test_counts_are_stored_compactly_and_exactly(tmp_path: Path, config: Config) -> None:
    store = SnapshotStore(tmp_path / "s.sqlite")
    build_snapshot(config, FakeExchange(), store, end=BUILT)
    rows = store._query("SELECT bid_c, volume, open_interest FROM candles LIMIT 1")
    assert all(isinstance(v, int) for v in rows[0] if v is not None)
    first = store.candles(A, 1, after=WINDOW, until=JUL_CLOSE)[0]
    assert first.yes_bid is not None and first.yes_bid.close == Decimal("0.4100")
    assert first.volume == Decimal("100.00")
