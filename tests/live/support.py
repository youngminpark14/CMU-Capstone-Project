"""Small recorded worlds for the live-mode tests, on file-backed state like the live modes have.

The fixture world is the episode tests' hawkish-minutes world (``tests.episodes.support``):
a December Fed event, its books, the October minutes (published 30 minutes before
``AS_OF``), a speech, and a scripted hawkish forecast that proposes a small NO
position. Here it is run as a live mode would run it: the state database and the
checkpoints are files under the configured data directory, and the clock starts an
hour before ``AS_OF`` so a first pass sees nothing new yet.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmfa.execution.paper import PaperExecutor
from pmfa.records.base import RunMode
from pmfa.records.decision import ReviewPolicy
from pmfa.records.market import OHLC, Candle
from pmfa.state.store import SqliteStore
from pmfa.world.live import live_paths
from pmfa.world.recorded import RecordedBooks, RecordedWorld, RecordedWorldFile
from tests.episodes.support import AS_OF, MINUTES, fed_file, hawkish_script, make_world

BEFORE = AS_OF - timedelta(hours=1)
"""The clock at the first pass: the minutes are not published yet."""


def live_file(data_dir: Path, **changes: Any) -> RecordedWorldFile:
    """The hawkish-minutes world with its data directory under ``data_dir`` (direct state transport)."""
    overrides: dict[str, Any] = {
        "paths": {"data_dir": str(data_dir)},
        "state": {"transport": "direct"},
    }
    overrides.update(changes.pop("config_overrides", {}))
    return fed_file(config_overrides=overrides, **changes)


def live_world(
    file: RecordedWorldFile,
    *,
    policy: ReviewPolicy = ReviewPolicy.FILL_TAGGED,
    mode: RunMode = RunMode.SHADOW,
    start: datetime | None = BEFORE,
    script: Any = None,
) -> RecordedWorld:
    """The recorded world with its store in the mode's state file, seeded on first use."""
    world = make_world(file, script or hawkish_script(), policy=policy, mode=mode)
    state_db, _ = live_paths(world.config, mode)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(state_db)
    executor = PaperExecutor(
        store,
        world.clock,
        world.config,
        world.exchange,
        RecordedBooks(file),
        bankroll=file.bankroll,
    )
    if not store.equity_series():
        store.set_stage(file.stage, "recorded world", file.as_of)
        executor.mark(BEFORE)
    if start is not None:
        world.clock.set(start)  # type: ignore[attr-defined]
    return dataclasses.replace(world, store=store, executor=executor)


def no_resolver(world: Any, *, now: datetime | None = None) -> list[str]:
    """Stands in for the outcome resolver: nothing resolves."""
    return []


def mid_candles(
    ticker: str, points: list[tuple[datetime, str]], spread: str = "0.02"
) -> list[Candle]:
    """One-minute candles whose YES mid closes at each given price."""
    half = Decimal(spread) / 2
    out = []
    for at, mid in points:
        m = Decimal(mid)
        bid = OHLC(open=m - half, high=m - half, low=m - half, close=m - half)
        ask = OHLC(open=m + half, high=m + half, low=m + half, close=m + half)
        out.append(Candle(ticker=ticker, end_ts=at, interval_minutes=1, yes_bid=bid, yes_ask=ask))
    return out


__all__ = ["AS_OF", "BEFORE", "MINUTES", "live_file", "live_world", "mid_candles", "no_resolver"]
