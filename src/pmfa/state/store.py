"""The SQLite implementation of the ``StateStore`` port.

One database file holds every durable record: episodes and their traces, tree
nodes, rejections, forecasts, proposals, reviews, orders and fills,
settlements, equity marks, the ledger, remembered documents, the halt flag,
and the stage.

One connection serves every thread. MCP tools run on worker threads (SDK 2.x)
or on the event loop (SDK 1.x), and a synchronous caller may drive the loop
from yet another thread, so the connection is opened with
``check_same_thread=False`` and every operation holds one lock.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pmfa.errors import NotFound, PmfaError
from pmfa.records.base import Stage
from pmfa.records.decision import (
    Fill,
    OrderResult,
    Proposal,
    ProposalStatus,
    Review,
    ReviewCard,
)
from pmfa.records.episode import (
    Calibration,
    EpisodeRecord,
    EpisodeStatus,
    HaltState,
    LedgerEntry,
    StageState,
    TraceStep,
)
from pmfa.records.forecast import Forecast, TreeNode
from pmfa.records.market import Side
from pmfa.records.roles import RejectionBrief
from pmfa.state.calibration import brier, expected_calibration_error
from pmfa.state.schema import DDL, SCHEMA_VERSION, parse_time, time_text

MEMORY = ":memory:"


class SchemaMismatch(PmfaError):
    """The database file was written by a different schema version."""


class SqliteStore:
    """The durable state: one SQLite database behind the ``StateStore`` protocol."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != MEMORY:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(DDL)
            self._check_version()

    def _check_version(self) -> None:
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
        elif row["version"] != SCHEMA_VERSION:
            raise SchemaMismatch(
                f"{self.path} has schema version {row['version']}, this code expects {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One locked transaction: committed on success, rolled back on error."""
        with self._lock, self._conn:
            yield self._conn

    def _rows(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- episodes and traces

    def save_episode(self, episode: EpisodeRecord) -> None:
        """Insert or replace an episode. Its trace is kept apart, through ``append_trace``."""
        stored = episode.model_copy(update={"trace": []})
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO episodes (episode_id, status, event_ticker, started_at, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    episode.episode_id,
                    episode.status.value,
                    episode.event_ticker,
                    time_text(episode.started_at),
                    stored.model_dump_json(),
                ),
            )

    def get_episode(self, episode_id: str) -> EpisodeRecord | None:
        rows = self._rows("SELECT json FROM episodes WHERE episode_id = ?", (episode_id,))
        if not rows:
            return None
        return self._with_trace(rows[0]["json"])

    def _with_trace(self, episode_json: str) -> EpisodeRecord:
        episode = EpisodeRecord.model_validate_json(episode_json)
        return episode.model_copy(update={"trace": self.get_trace(episode.episode_id)})

    def list_episodes(
        self,
        *,
        status: EpisodeStatus | None = None,
        event_ticker: str | None = None,
        limit: int = 100,
    ) -> list[EpisodeRecord]:
        """Episodes newest first (by start time), each with its full trace."""
        sql = "SELECT json FROM episodes WHERE 1 = 1"
        params: list[object] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(EpisodeStatus(status).value)
        if event_ticker is not None:
            sql += " AND event_ticker = ?"
            params.append(event_ticker)
        sql += " ORDER BY started_at DESC, episode_id LIMIT ?"
        params.append(limit)
        return [self._with_trace(row["json"]) for row in self._rows(sql, tuple(params))]

    def append_trace(self, episode_id: str, step: TraceStep) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO trace_steps (episode_id, json) VALUES (?, ?)",
                (episode_id, step.model_dump_json()),
            )

    def get_trace(self, episode_id: str) -> list[TraceStep]:
        """The trace in the order its steps were appended."""
        rows = self._rows(
            "SELECT json FROM trace_steps WHERE episode_id = ? ORDER BY id", (episode_id,)
        )
        return [TraceStep.model_validate_json(row["json"]) for row in rows]

    # -- scenario tree

    def put_node(self, node: TreeNode) -> None:
        """Insert or update a node. An update keeps the node's original position."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO nodes (node_id, episode_id, depth, status, json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (node_id) DO UPDATE SET episode_id = excluded.episode_id, "
                "depth = excluded.depth, status = excluded.status, json = excluded.json",
                (
                    node.node_id,
                    node.episode_id,
                    node.depth,
                    node.status.value,
                    node.model_dump_json(),
                ),
            )

    def get_nodes(self, episode_id: str) -> list[TreeNode]:
        """An episode's nodes by depth, then in the order they were first written."""
        rows = self._rows(
            "SELECT json FROM nodes WHERE episode_id = ? ORDER BY depth, rowid", (episode_id,)
        )
        return [TreeNode.model_validate_json(row["json"]) for row in rows]

    def record_rejection(self, episode_id: str, event_ticker: str, claim: str, reason: str) -> None:
        """Remember a critic rejection. The same claim and reason for an event is kept once."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rejections (episode_id, event_ticker, claim, reason) "
                "VALUES (?, ?, ?, ?)",
                (episode_id, event_ticker, claim, reason),
            )

    def get_rejections(self, event_ticker: str) -> list[RejectionBrief]:
        rows = self._rows(
            "SELECT claim, reason FROM rejections WHERE event_ticker = ? ORDER BY id",
            (event_ticker,),
        )
        return [RejectionBrief(claim=row["claim"], reason=row["reason"]) for row in rows]

    # -- forecasts, proposals, reviews

    def save_forecast(self, forecast: Forecast) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO forecasts "
                "(forecast_id, episode_id, event_ticker, system, as_of, json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    forecast.forecast_id,
                    forecast.episode_id,
                    forecast.event_ticker,
                    forecast.system,
                    time_text(forecast.as_of),
                    forecast.model_dump_json(),
                ),
            )

    def last_forecast_time(self, event_ticker: str) -> datetime | None:
        """The as-of time of the event's latest pipeline forecast. Baselines do not count."""
        rows = self._rows(
            "SELECT max(as_of) AS at FROM forecasts WHERE event_ticker = ? AND system = 'pipeline'",
            (event_ticker,),
        )
        at = rows[0]["at"]
        return parse_time(at) if at is not None else None

    def save_proposal(
        self, proposal: Proposal, status: ProposalStatus, card: ReviewCard | None = None
    ) -> None:
        """Insert or replace a proposal. A save without a card keeps the card already stored."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO proposals (proposal_id, episode_id, status, json, card_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (proposal_id) DO UPDATE SET episode_id = excluded.episode_id, "
                "status = excluded.status, json = excluded.json, "
                "card_json = coalesce(excluded.card_json, proposals.card_json)",
                (
                    proposal.proposal_id,
                    proposal.episode_id,
                    ProposalStatus(status).value,
                    proposal.model_dump_json(),
                    card.model_dump_json() if card is not None else None,
                ),
            )

    def get_proposal(
        self, proposal_id: str
    ) -> tuple[Proposal, ProposalStatus, ReviewCard | None] | None:
        rows = self._rows(
            "SELECT json, status, card_json FROM proposals WHERE proposal_id = ?", (proposal_id,)
        )
        if not rows:
            return None
        row = rows[0]
        card = ReviewCard.model_validate_json(row["card_json"]) if row["card_json"] else None
        return Proposal.model_validate_json(row["json"]), ProposalStatus(row["status"]), card

    def list_proposals(
        self, status: ProposalStatus | None = None
    ) -> list[tuple[Proposal, ProposalStatus]]:
        """Proposals in the order they were first saved."""
        if status is None:
            rows = self._rows("SELECT json, status FROM proposals ORDER BY rowid")
        else:
            rows = self._rows(
                "SELECT json, status FROM proposals WHERE status = ? ORDER BY rowid",
                (ProposalStatus(status).value,),
            )
        return [
            (Proposal.model_validate_json(row["json"]), ProposalStatus(row["status"]))
            for row in rows
        ]

    def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE proposals SET status = ? WHERE proposal_id = ?",
                (ProposalStatus(status).value, proposal_id),
            )
            if cursor.rowcount == 0:
                raise NotFound(f"no proposal {proposal_id!r}")

    def save_review(self, review: Review) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO reviews (proposal_id, json) VALUES (?, ?)",
                (review.proposal_id, review.model_dump_json()),
            )

    def list_reviews(self) -> list[Review]:
        rows = self._rows("SELECT json FROM reviews ORDER BY id")
        return [Review.model_validate_json(row["json"]) for row in rows]

    # -- orders, fills, settlements

    def save_order(self, order: OrderResult) -> None:
        """Insert or replace an order and its fills."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO orders (order_id, episode_id, market_ticker, json) "
                "VALUES (?, ?, ?, ?)",
                (order.order_id, order.episode_id, order.market_ticker, order.model_dump_json()),
            )
            conn.execute("DELETE FROM fills WHERE order_id = ?", (order.order_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO fills (fill_id, order_id, market_ticker, filled_at, json) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        fill.fill_id,
                        order.order_id,
                        fill.market_ticker,
                        time_text(fill.filled_at),
                        fill.model_dump_json(),
                    )
                    for fill in order.fills
                ],
            )

    def list_orders(self, *, episode_id: str | None = None) -> list[OrderResult]:
        if episode_id is None:
            rows = self._rows("SELECT json FROM orders ORDER BY rowid")
        else:
            rows = self._rows(
                "SELECT json FROM orders WHERE episode_id = ? ORDER BY rowid", (episode_id,)
            )
        return [OrderResult.model_validate_json(row["json"]) for row in rows]

    def list_fills(self, *, market_ticker: str | None = None) -> list[Fill]:
        """Fills oldest first."""
        if market_ticker is None:
            rows = self._rows("SELECT json FROM fills ORDER BY filled_at, rowid")
        else:
            rows = self._rows(
                "SELECT json FROM fills WHERE market_ticker = ? ORDER BY filled_at, rowid",
                (market_ticker,),
            )
        return [Fill.model_validate_json(row["json"]) for row in rows]

    def record_settlement(self, market_ticker: str, result: Side, settled_at: datetime) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settlements (market_ticker, result, settled_at) "
                "VALUES (?, ?, ?)",
                (market_ticker, Side(result).value, time_text(settled_at)),
            )

    def list_settlements(self) -> list[tuple[str, Side, datetime]]:
        rows = self._rows(
            "SELECT market_ticker, result, settled_at FROM settlements "
            "ORDER BY settled_at, market_ticker"
        )
        return [
            (row["market_ticker"], Side(row["result"]), parse_time(row["settled_at"]))
            for row in rows
        ]

    def record_equity(self, at: datetime, equity: Decimal) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity (at, equity) VALUES (?, ?)",
                (time_text(at), str(Decimal(equity))),
            )

    def equity_series(self) -> list[tuple[datetime, Decimal]]:
        rows = self._rows("SELECT at, equity FROM equity ORDER BY at")
        return [(parse_time(row["at"]), Decimal(row["equity"])) for row in rows]

    # -- ledger and calibration

    def add_ledger_entries(self, entries: list[LedgerEntry]) -> None:
        """Insert or replace ledger entries by id."""
        with self._write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO ledger "
                "(entry_id, episode_id, market_ticker, category, system, resolved_at, json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.entry_id,
                        e.episode_id,
                        e.market_ticker,
                        e.category,
                        e.system,
                        time_text(e.resolved_at) if e.resolved_at is not None else None,
                        e.model_dump_json(),
                    )
                    for e in entries
                ],
            )

    def resolve_market(self, market_ticker: str, outcome: bool, resolved_at: datetime) -> int:
        """Write the outcome and Brier scores onto every unresolved entry of a market.

        ``brier`` is ``(probability - outcome)^2``; ``market_brier`` is the same
        for the market price, when the entry has one. Returns how many entries changed.
        """
        resolved_text = time_text(resolved_at)
        with self._write() as conn:
            rows = conn.execute(
                "SELECT entry_id, json FROM ledger WHERE market_ticker = ?", (market_ticker,)
            ).fetchall()
            updated = 0
            for row in rows:
                entry = LedgerEntry.model_validate_json(row["json"])
                if entry.outcome is not None:
                    continue
                resolved = entry.model_copy(
                    update={
                        "outcome": outcome,
                        "resolved_at": resolved_at,
                        "brier": brier(entry.probability, outcome),
                        "market_brier": (
                            brier(entry.market_price, outcome)
                            if entry.market_price is not None
                            else None
                        ),
                    }
                )
                # model_copy skips validation; round-trip so resolved_at is normalized to UTC.
                resolved = LedgerEntry.model_validate(resolved.model_dump())
                conn.execute(
                    "UPDATE ledger SET resolved_at = ?, json = ? WHERE entry_id = ?",
                    (resolved_text, resolved.model_dump_json(), row["entry_id"]),
                )
                updated += 1
        return updated

    def ledger(
        self,
        *,
        resolved_before: datetime | None = None,
        category: str | None = None,
        system: str | None = None,
    ) -> list[LedgerEntry]:
        """Ledger entries in the order they were added.

        With ``resolved_before``, only entries resolved strictly before that time.
        """
        sql = "SELECT json FROM ledger WHERE 1 = 1"
        params: list[object] = []
        if resolved_before is not None:
            sql += " AND resolved_at IS NOT NULL AND resolved_at < ?"
            params.append(time_text(resolved_before))
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if system is not None:
            sql += " AND system = ?"
            params.append(system)
        sql += " ORDER BY rowid"
        return [
            LedgerEntry.model_validate_json(row["json"]) for row in self._rows(sql, tuple(params))
        ]

    def calibration(self, category: str, *, as_of: datetime) -> Calibration:
        """Calibration of the pipeline's forecasts in a category, resolved before ``as_of``."""
        entries = self.ledger(resolved_before=as_of, category=category, system="pipeline")
        pairs = [(e.probability, e.outcome) for e in entries if e.outcome is not None]
        return Calibration(
            category=category,
            n_resolved=len(pairs),
            calibration_error=expected_calibration_error(pairs),
        )

    # -- sources behind open positions

    def remember_document(self, doc_id: str, content_hash: str, seen_at: datetime) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO documents (doc_id, content_hash, seen_at) VALUES (?, ?, ?)",
                (doc_id, content_hash, time_text(seen_at)),
            )

    def document_hash(self, doc_id: str) -> str | None:
        rows = self._rows("SELECT content_hash FROM documents WHERE doc_id = ?", (doc_id,))
        return rows[0]["content_hash"] if rows else None

    # -- halt and stage

    def _get_flag(self, name: str) -> str | None:
        rows = self._rows("SELECT json FROM flags WHERE name = ?", (name,))
        return rows[0]["json"] if rows else None

    def _set_flag(self, name: str, json_text: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO flags (name, json) VALUES (?, ?)", (name, json_text)
            )

    def get_halt(self) -> HaltState:
        """The halt flag; not halted until someone sets it."""
        stored = self._get_flag("halt")
        return HaltState.model_validate_json(stored) if stored else HaltState(halted=False)

    def set_halt(self, reason: str, at: datetime) -> None:
        self._set_flag("halt", HaltState(halted=True, reason=reason, at=at).model_dump_json())

    def clear_halt(self) -> None:
        self._set_flag("halt", HaltState(halted=False).model_dump_json())

    def get_stage(self) -> StageState:
        """The autonomy stage; backtest until someone advances it."""
        stored = self._get_flag("stage")
        return (
            StageState.model_validate_json(stored) if stored else StageState(stage=Stage.BACKTEST)
        )

    def set_stage(self, stage: Stage, reason: str, at: datetime) -> None:
        self._set_flag(
            "stage", StageState(stage=Stage(stage), reason=reason, at=at).model_dump_json()
        )
