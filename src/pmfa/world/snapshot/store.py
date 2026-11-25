"""The snapshot file: one SQLite database of series, events, markets, candles and a manifest.

Tables
------
- ``series``: one row per followed series, the ``Series`` record as JSON.
- ``events``: one row per event, the ``Event`` record as JSON *without* its
  markets (they live in ``markets``), plus its series and close time.
- ``markets``: one row per market, the final ``Market`` record as JSON (as it
  stood when the snapshot was built, result included), plus open, close and
  settlement times (Unix seconds) and the result as columns. ``id`` is a small
  integer that the candle rows use instead of repeating the ticker.
- ``candles``: one row per (market, interval in minutes, end time). Prices are
  stored as integers in ten-thousandths of a dollar (the exchange's four-decimal
  fixed point) and counts as integers in hundredths (its two-decimal fixed
  point), so a row is a handful of integers. An empty side is ``NULL``. The
  primary key is (market, interval, end time), so every read by ticker,
  interval and time range is an index range scan.
- ``coverage``: which (market, interval) ranges have been fetched completely.
  A range is written in the same transaction as its candles, which is what
  makes an interrupted build resumable without gaps.
- ``meta``: the manifest, as JSON under the key ``manifest``.

The store knows nothing about clocks: it returns what is stored. The as-of rule
is applied by :class:`pmfa.world.snapshot.reader.SnapshotExchange` and
:func:`pmfa.world.snapshot.book.execution_book`, which pass explicit time bounds.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pmfa.errors import NotFound
from pmfa.records.base import Record, UtcTime
from pmfa.records.market import OHLC, Candle, Event, Market, Series

if TYPE_CHECKING:
    from pmfa.config import Config

SNAPSHOT_FILE = "snapshot.sqlite"
INTERVALS = (1, 60)
"""Candle intervals a snapshot holds, in minutes."""

_PRICE_SCALE = Decimal("10000")
_COUNT_SCALE = Decimal("100")
_COUNT_QUANTUM = Decimal("0.01")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    ticker TEXT PRIMARY KEY,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_ticker TEXT PRIMARY KEY,
    series_ticker TEXT NOT NULL,
    close_ts INTEGER NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_series ON events (series_ticker, close_ts);
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    event_ticker TEXT NOT NULL,
    open_ts INTEGER NOT NULL,
    close_ts INTEGER NOT NULL,
    settled_ts INTEGER,
    result TEXT,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS markets_by_event ON markets (event_ticker);
CREATE TABLE IF NOT EXISTS candles (
    market_id INTEGER NOT NULL,
    interval INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    bid_o INTEGER, bid_h INTEGER, bid_l INTEGER, bid_c INTEGER,
    ask_o INTEGER, ask_h INTEGER, ask_l INTEGER, ask_c INTEGER,
    px_o INTEGER, px_h INTEGER, px_l INTEGER, px_c INTEGER,
    volume INTEGER NOT NULL,
    open_interest INTEGER,
    PRIMARY KEY (market_id, interval, end_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS coverage (
    market_id INTEGER NOT NULL,
    interval INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    PRIMARY KEY (market_id, interval)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CANDLE_COLUMNS = (
    "end_ts, bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c, "
    "px_o, px_h, px_l, px_c, volume, open_interest"
)


class SnapshotManifest(Record):
    """What a snapshot holds and how it was built."""

    built_at: UtcTime
    window_start: UtcTime
    """Events closing at or after this are included; candles reach back from it (see the builder)."""
    window_end: UtcTime
    """The build date: events closing after it were not yet closed and are left out."""
    series: list[str]
    historical_cutoff: UtcTime | None = None
    """The exchange's ``market_settled_ts`` when the snapshot was built."""
    counts: dict[str, int] = {}
    """``events``, ``markets``, ``candles_1m``, ``candles_60m``."""
    minute_days: int | None = None
    """``None``: one-minute candles from the window start; ``N``: only the last N days before close."""
    minute_markets: int = 0
    """Markets whose one-minute range was fetched completely."""
    minute_span_days: float = 0.0
    """Total days of one-minute range fetched, summed over markets."""
    source_host: str = ""
    elapsed_seconds: float = 0.0
    dropped_events: dict[str, str] | None = None
    """Events the exchange listed that the builder did not store, with why (no markets, or a
    close time outside the window). ``None``: built before the builder recorded them."""


def snapshot_path(config: Config) -> Path:
    """Where the snapshot lives: ``<data_dir>/snapshot/snapshot.sqlite``."""
    return config.paths.snapshot_dir() / SNAPSHOT_FILE


def to_ts(moment: datetime) -> int:
    """Whole Unix seconds, rounded down."""
    return int(moment.timestamp() // 1)


def from_ts(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _ceil_ts(moment: datetime) -> int:
    whole = to_ts(moment)
    return whole if whole == moment.timestamp() else whole + 1


def _enc_price(value: Decimal) -> int:
    return int((value * _PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN))


def _enc_count(value: Decimal) -> int:
    return int((value.quantize(_COUNT_QUANTUM, ROUND_HALF_EVEN) * _COUNT_SCALE).to_integral_value())


def _dec_price(value: int) -> Decimal:
    return Decimal(value).scaleb(-4)


def _dec_count(value: int) -> Decimal:
    return Decimal(value).scaleb(-2)


def _enc_ohlc(ohlc: OHLC | None) -> tuple[int | None, ...]:
    if ohlc is None:
        return (None, None, None, None)
    return tuple(_enc_price(v) for v in (ohlc.open, ohlc.high, ohlc.low, ohlc.close))


def _dec_ohlc(values: tuple[int | None, ...]) -> OHLC | None:
    if any(v is None for v in values):
        return None
    o, h, low, c = (_dec_price(v) for v in values)  # type: ignore[arg-type]
    return OHLC(open=o, high=h, low=low, close=c)


def _row_to_candle(ticker: str, interval: int, row: tuple) -> Candle:
    return Candle(
        ticker=ticker,
        end_ts=from_ts(row[0]),
        interval_minutes=interval,
        yes_bid=_dec_ohlc(row[1:5]),
        yes_ask=_dec_ohlc(row[5:9]),
        price=_dec_ohlc(row[9:13]),
        volume=_dec_count(row[13]),
        open_interest=None if row[14] is None else _dec_count(row[14]),
    )


class SnapshotStore:
    """One SQLite snapshot file. Safe to share between threads (one connection, one lock)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._ids: dict[str, int] = {}
        self._markets: dict[str, Market] = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _query(self, sql: str, params: Iterable[object] = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    # -- writing -------------------------------------------------------------------------

    def put_series(self, series: Series) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO series (ticker, record) VALUES (?, ?)",
                (series.ticker, series.model_dump_json()),
            )

    def put_event(self, event: Event) -> None:
        """Insert or replace an event and its markets (the records as given, results included)."""
        bare = event.model_copy(update={"markets": []})
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO events (event_ticker, series_ticker, close_ts, record) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_ticker,
                    event.series_ticker,
                    to_ts(event.close_time),
                    bare.model_dump_json(),
                ),
            )
            for m in event.markets:
                db.execute(
                    "INSERT INTO markets (ticker, event_ticker, open_ts, close_ts, settled_ts, "
                    "result, record) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (ticker) DO UPDATE SET event_ticker = excluded.event_ticker, "
                    "open_ts = excluded.open_ts, close_ts = excluded.close_ts, "
                    "settled_ts = excluded.settled_ts, result = excluded.result, "
                    "record = excluded.record",
                    (
                        m.ticker,
                        m.event_ticker,
                        to_ts(m.open_time),
                        to_ts(m.close_time),
                        None if m.settled_at is None else _ceil_ts(m.settled_at),
                        None if m.result is None else m.result.value,
                        m.model_dump_json(),
                    ),
                )
                self._markets.pop(m.ticker, None)

    def put_candles(
        self,
        ticker: str,
        interval: int,
        candles: Iterable[Candle],
        *,
        start: datetime,
        end: datetime,
    ) -> int:
        """Replace a market's candles of one interval and mark ``(start, end]`` complete.

        Candles and the coverage row are written in one transaction: either the
        whole range is stored and marked, or nothing is.
        """
        if interval not in INTERVALS:
            raise ValueError(f"a snapshot holds intervals {INTERVALS}, not {interval}")
        market_id = self._market_id(ticker)
        rows = [
            (
                market_id,
                interval,
                to_ts(c.end_ts),
                *_enc_ohlc(c.yes_bid),
                *_enc_ohlc(c.yes_ask),
                *_enc_ohlc(c.price),
                _enc_count(c.volume),
                None if c.open_interest is None else _enc_count(c.open_interest),
            )
            for c in candles
        ]
        with self._tx() as db:
            db.execute(
                "DELETE FROM candles WHERE market_id = ? AND interval = ?", (market_id, interval)
            )
            db.executemany(
                f"INSERT OR REPLACE INTO candles (market_id, interval, {_CANDLE_COLUMNS}) "
                f"VALUES ({', '.join('?' * 17)})",
                rows,
            )
            db.execute(
                "INSERT OR REPLACE INTO coverage (market_id, interval, start_ts, end_ts) "
                "VALUES (?, ?, ?, ?)",
                (market_id, interval, to_ts(start), to_ts(end)),
            )
        return len(rows)

    def write_manifest(self, manifest: SnapshotManifest) -> SnapshotManifest:
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('manifest', ?)",
                (manifest.model_dump_json(),),
            )
        return manifest

    # -- reading reference data ------------------------------------------------------------

    def manifest(self) -> SnapshotManifest | None:
        rows = self._query("SELECT value FROM meta WHERE key = 'manifest'")
        return SnapshotManifest.model_validate_json(rows[0][0]) if rows else None

    def get_series(self, ticker: str) -> Series:
        rows = self._query("SELECT record FROM series WHERE ticker = ?", (ticker,))
        if not rows:
            raise NotFound(f"series {ticker} is not in the snapshot")
        return Series.model_validate_json(rows[0][0])

    def series_tickers(self) -> list[str]:
        return [r[0] for r in self._query("SELECT ticker FROM series ORDER BY ticker")]

    def event_tickers(self, series_ticker: str | None = None) -> list[str]:
        """Event tickers, earliest close first, optionally of one series."""
        if series_ticker is None:
            rows = self._query("SELECT event_ticker FROM events ORDER BY close_ts, event_ticker")
        else:
            rows = self._query(
                "SELECT event_ticker FROM events WHERE series_ticker = ? "
                "ORDER BY close_ts, event_ticker",
                (series_ticker,),
            )
        return [r[0] for r in rows]

    def get_event(self, event_ticker: str) -> Event:
        """The event with its final market records (no as-of rule: see the reader)."""
        rows = self._query("SELECT record FROM events WHERE event_ticker = ?", (event_ticker,))
        if not rows:
            raise NotFound(f"event {event_ticker} is not in the snapshot")
        bare = Event.model_validate_json(rows[0][0])
        tickers = [
            r[0]
            for r in self._query(
                "SELECT ticker FROM markets WHERE event_ticker = ? ORDER BY close_ts, ticker",
                (event_ticker,),
            )
        ]
        return bare.model_copy(update={"markets": [self.get_market(t) for t in tickers]})

    def get_market(self, ticker: str) -> Market:
        """The final market record as built (no as-of rule: see the reader)."""
        cached = self._markets.get(ticker)
        if cached is not None:
            return cached
        rows = self._query("SELECT record FROM markets WHERE ticker = ?", (ticker,))
        if not rows:
            raise NotFound(f"market {ticker} is not in the snapshot")
        market = Market.model_validate_json(rows[0][0])
        self._markets[ticker] = market
        return market

    def has_market(self, ticker: str) -> bool:
        return bool(self._query("SELECT 1 FROM markets WHERE ticker = ?", (ticker,)))

    # -- reading candles -------------------------------------------------------------------

    def coverage(self, ticker: str, interval: int) -> tuple[datetime, datetime] | None:
        """The ``(start, end]`` range fetched completely for this market and interval, if any."""
        rows = self._query(
            "SELECT start_ts, end_ts FROM coverage WHERE market_id = ? AND interval = ?",
            (self._market_id(ticker), interval),
        )
        return (from_ts(rows[0][0]), from_ts(rows[0][1])) if rows else None

    def is_complete(self, ticker: str, interval: int, start: datetime, end: datetime) -> bool:
        """True when ``(start, end]`` lies inside a range already fetched completely."""
        span = self.coverage(ticker, interval)
        return span is not None and span[0] <= start and span[1] >= end

    def candles(
        self, ticker: str, interval: int, *, after: datetime, until: datetime
    ) -> list[Candle]:
        """Candles ending in ``(after, until]``, oldest first."""
        rows = self._query(
            f"SELECT {_CANDLE_COLUMNS} FROM candles WHERE market_id = ? AND interval = ? "
            "AND end_ts > ? AND end_ts <= ? ORDER BY end_ts",
            (self._market_id(ticker), interval, to_ts(after), to_ts(until)),
        )
        return [_row_to_candle(ticker, interval, r) for r in rows]

    def latest_candle(
        self, ticker: str, interval: int, *, until: datetime, traded: bool = False
    ) -> Candle | None:
        """The last candle ending at or before ``until`` (``traded``: the last one with a trade price)."""
        extra = " AND px_c IS NOT NULL" if traded else ""
        rows = self._query(
            f"SELECT {_CANDLE_COLUMNS} FROM candles WHERE market_id = ? AND interval = ? "
            f"AND end_ts <= ?{extra} ORDER BY end_ts DESC LIMIT 1",
            (self._market_id(ticker), interval, to_ts(until)),
        )
        return _row_to_candle(ticker, interval, rows[0]) if rows else None

    def first_candle(
        self, ticker: str, interval: int, *, after: datetime, until: datetime
    ) -> Candle | None:
        """The first candle ending in ``(after, until]``."""
        rows = self._query(
            f"SELECT {_CANDLE_COLUMNS} FROM candles WHERE market_id = ? AND interval = ? "
            "AND end_ts > ? AND end_ts <= ? ORDER BY end_ts LIMIT 1",
            (self._market_id(ticker), interval, to_ts(after), to_ts(until)),
        )
        return _row_to_candle(ticker, interval, rows[0]) if rows else None

    def volume(self, ticker: str, interval: int, *, after: datetime, until: datetime) -> Decimal:
        """Contracts traded in candles ending in ``(after, until]``."""
        rows = self._query(
            "SELECT COALESCE(SUM(volume), 0) FROM candles WHERE market_id = ? AND interval = ? "
            "AND end_ts > ? AND end_ts <= ?",
            (self._market_id(ticker), interval, to_ts(after), to_ts(until)),
        )
        return _dec_count(rows[0][0])

    # -- counts ----------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        events = self._query("SELECT COUNT(*) FROM events")[0][0]
        markets = self._query("SELECT COUNT(*) FROM markets")[0][0]
        out = {"events": events, "markets": markets}
        for interval in INTERVALS:
            out[f"candles_{interval}m"] = self._query(
                "SELECT COUNT(*) FROM candles WHERE interval = ?", (interval,)
            )[0][0]
        return out

    def coverage_summary(self, interval: int) -> tuple[int, float]:
        """Markets with a complete range for ``interval``, and the total days those ranges span."""
        rows = self._query(
            "SELECT COUNT(*), COALESCE(SUM(end_ts - start_ts), 0) FROM coverage WHERE interval = ?",
            (interval,),
        )
        return rows[0][0], rows[0][1] / 86400

    # -- internals -------------------------------------------------------------------------

    def _market_id(self, ticker: str) -> int:
        found = self._ids.get(ticker)
        if found is None:
            rows = self._query("SELECT id FROM markets WHERE ticker = ?", (ticker,))
            if not rows:
                raise NotFound(f"market {ticker} is not in the snapshot")
            found = int(rows[0][0])
            self._ids[ticker] = found
        return found


def manifest_json(manifest: SnapshotManifest) -> str:
    """The manifest as indented JSON, for the CLI to print."""
    return json.dumps(manifest.model_dump(mode="json"), indent=2)


__all__ = [
    "INTERVALS",
    "SNAPSHOT_FILE",
    "SnapshotManifest",
    "SnapshotStore",
    "from_ts",
    "manifest_json",
    "snapshot_path",
    "to_ts",
]
