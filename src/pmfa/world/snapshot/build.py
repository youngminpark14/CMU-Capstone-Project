"""Fill a snapshot from the exchange: events, markets, results, hourly and one-minute candles.

What is fetched, for every enabled series in ``config.follow``:

- every event whose close time is in ``[start, end]`` (``start`` defaults to
  ``config.evaluation.window_start``; ``end`` is the build time), with its
  markets, rules and results;
- **hourly** candles for each market over ``(max(open, start - 30 days), close]``,
  so every forecast in the window has a month of price history behind it;
- **one-minute** candles over ``(max(open, start), close]``; ``minute_days=N``
  narrows this to the last N days before close.

The exchange argument is anything with :class:`~pmfa.world.kalshi.data.KalshiData`'s
reading methods (:class:`SnapshotSource`). In real use that is
``production_exchange(config, clock)``, which routes each read to the live or
the historical endpoints and throttles; this module adds no parallelism.

Resumable: a (market, interval) range is stored and marked complete in one
transaction, and a range already marked complete is skipped. Re-running after
an interruption therefore continues with the first market that was not done.
Event and market records are always re-read (a few requests per series), so a
market that settled since the last run gets its result.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time as dtime, timedelta
from typing import TYPE_CHECKING, Protocol

from pmfa.records.market import Candle, Event, Series
from pmfa.world.snapshot.store import SnapshotManifest, SnapshotStore

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.world.kalshi.parse import HistoricalCutoff

HOURLY_LOOKBACK = timedelta(days=30)
"""Hourly candles reach this far before the window start."""

Progress = Callable[[str], None]


class SnapshotSource(Protocol):
    """The reading methods of ``KalshiData`` that the builder uses."""

    def get_series(self, series_ticker: str) -> Series: ...

    def iter_series_events(
        self, series_ticker: str, *, closing_from: datetime, closing_to: datetime
    ) -> Iterator[Event]: ...

    def get_candles(
        self, ticker: str, *, start: datetime, end: datetime, interval_minutes: int
    ) -> list[Candle]: ...

    def historical_cutoff(self) -> HistoricalCutoff: ...


def window_start(config: Config) -> datetime:
    """``config.evaluation.window_start`` as midnight UTC."""
    return datetime.combine(config.evaluation.window_start, dtime(0), tzinfo=UTC)


def _host(exchange: object) -> str:
    client = getattr(exchange, "client", None)
    return str(getattr(client, "host", "") or "")


def build_snapshot(
    config: Config,
    exchange: SnapshotSource,
    store: SnapshotStore,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    minute_days: int | None = None,
    progress: Progress | None = None,
) -> SnapshotManifest:
    """Fetch the followed series into ``store`` and write its manifest. See the module docstring.

    ``end`` defaults to the system time (this is an adapter, run from the
    command line). ``progress`` receives one line per market and interval.
    """
    began = time.monotonic()
    say = progress or (lambda _line: None)
    start = window_start(config) if start is None else start.astimezone(UTC)
    end = datetime.now(UTC) if end is None else end.astimezone(UTC)
    if minute_days is not None and minute_days < 1:
        raise ValueError("minute_days must be at least 1")
    cutoff = exchange.historical_cutoff()
    series_tickers = [s.ticker for s in config.follow.enabled_series()]

    for series_ticker in series_tickers:
        store.put_series(exchange.get_series(series_ticker))
        events = list(
            exchange.iter_series_events(series_ticker, closing_from=start, closing_to=end)
        )
        say(f"{series_ticker}: {len(events)} event(s) closing {start:%Y-%m-%d} to {end:%Y-%m-%d}")
        for event in events:
            store.put_event(event)
            for market in event.markets:
                hourly_from = max(market.open_time, start - HOURLY_LOOKBACK)
                minute_from = max(market.open_time, start)
                if minute_days is not None:
                    minute_from = max(minute_from, market.close_time - timedelta(days=minute_days))
                for interval, lo in ((60, hourly_from), (1, minute_from)):
                    _fetch_range(exchange, store, market.ticker, interval, lo, market.close_time, say)

    hourly = store.coverage_summary(60)
    minute = store.coverage_summary(1)
    manifest = SnapshotManifest(
        built_at=datetime.now(UTC),
        window_start=start,
        window_end=end,
        series=series_tickers,
        historical_cutoff=cutoff.market_settled_ts,
        counts=store.counts(),
        minute_days=minute_days,
        minute_markets=minute[0],
        minute_span_days=round(minute[1], 2),
        source_host=_host(exchange),
        elapsed_seconds=round(time.monotonic() - began, 1),
    )
    say(f"done: {manifest.counts}, hourly ranges {hourly[0]}, minute ranges {minute[0]}")
    return store.write_manifest(manifest)


def _fetch_range(
    exchange: SnapshotSource,
    store: SnapshotStore,
    ticker: str,
    interval: int,
    lo: datetime,
    hi: datetime,
    say: Progress,
) -> None:
    label = f"{ticker} {interval}m"
    if hi <= lo:
        store.put_candles(ticker, interval, [], start=lo, end=lo)
        return
    if store.is_complete(ticker, interval, lo, hi):
        say(f"{label}: already complete, skipped")
        return
    candles = exchange.get_candles(ticker, start=lo, end=hi, interval_minutes=interval)
    stored = store.put_candles(ticker, interval, candles, start=lo, end=hi)
    say(f"{label}: {stored} candle(s), {lo:%Y-%m-%d %H:%M} to {hi:%Y-%m-%d %H:%M}")


__all__ = ["HOURLY_LOOKBACK", "SnapshotSource", "build_snapshot", "window_start"]
