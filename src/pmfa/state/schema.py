"""The SQLite schema of the state database.

Every record is stored whole as JSON text, next to the few columns needed to
find it. Times in columns are fixed-width UTC text, so string order is time
order. ``schema_version`` lets a later version refuse, or migrate, an older file.
"""

from __future__ import annotations

from datetime import UTC, datetime

SCHEMA_VERSION = 1

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    event_ticker TEXT,
    started_at TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_steps_episode ON trace_steps (episode_id, id);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_episode ON nodes (episode_id, depth);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    claim TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE (event_ticker, claim, reason)
);
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    system TEXT NOT NULL,
    as_of TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    status TEXT NOT NULL,
    json TEXT NOT NULL,
    card_json TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    market_ticker TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    settled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    at TEXT PRIMARY KEY,
    equity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    entry_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    category TEXT NOT NULL,
    system TEXT NOT NULL,
    resolved_at TEXT,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_market ON ledger (market_ticker);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (
    name TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
"""
"""``flags`` holds the halt state and the stage, one row each."""


def time_text(moment: datetime) -> str:
    """The storage form of a time. Refuses naive times so a comparison is never ambiguous."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"naive datetime {moment!r}: the state store holds timezone-aware times")
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)
