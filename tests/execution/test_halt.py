"""Halt: the persisted flag, and each self-halt condition on its own."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pmfa.config import Config
from pmfa.execution.halt import enforce, halt, resume, stop_reasons
from pmfa.records.decision import OrderStatus
from pmfa.records.episode import LedgerEntry
from pmfa.records.market import Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.clocks import SimClock
from pmfa.world.recorded import RecordedEvidence
from tests.world.recorded.factories import HOLD, MINUTES, world_file

from .factories import CONFIG, episode, fill, order, paper

NOW = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)  # 15:00 in New York
EVIDENCE = RecordedEvidence(world_file())


def reasons(store: SqliteStore, config: Config = CONFIG, evidence=EVIDENCE) -> list[str]:
    return stop_reasons(store, config, SimClock(NOW), evidence)


def names(found: list[str]) -> list[str]:
    return [r.split(":")[0] for r in found]


def test_halt_sets_the_flag_and_it_persists(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    executor, _, _ = paper()
    with SqliteStore(path) as store:
        assert halt(store, executor, SimClock(NOW), "manual stop") == 0
        assert store.get_halt().halted
    with SqliteStore(path) as reopened:
        state = reopened.get_halt()
        assert (state.halted, state.reason, state.at) == (True, "manual stop", NOW)
        resume(reopened)
    with SqliteStore(path) as again:
        assert not again.get_halt().halted


def test_nothing_holds_on_a_quiet_store() -> None:
    assert reasons(SqliteStore(MEMORY)) == []


def test_daily_loss_stop_alone() -> None:
    store = SqliteStore(MEMORY)
    store.record_equity(NOW - timedelta(days=1), Decimal("1000"))
    store.record_equity(NOW - timedelta(hours=2), Decimal("965"))  # -3.5% today
    assert names(reasons(store)) == ["daily loss stop"]


def test_drawdown_alone() -> None:
    store = SqliteStore(MEMORY)
    for day, equity in enumerate(["1000", "980", "960", "940", "920", "900"]):
        store.record_equity(NOW - timedelta(days=5 - day), Decimal(equity))
    assert names(reasons(store)) == ["drawdown"]


def test_fill_failures_more_than_one_in_ten() -> None:
    store = SqliteStore(MEMORY)
    for i in range(9):
        store.save_order(order(f"o{i}", OrderStatus.UNFILLED if i < 5 else OrderStatus.FILLED))
    assert reasons(store) == []  # fewer than ten orders: not judged yet
    store.save_order(order("o9", OrderStatus.FILLED))
    assert names(reasons(store)) == ["fill failures"]

    one = SqliteStore(MEMORY)
    for i in range(10):
        one.save_order(order(f"o{i}", OrderStatus.REJECTED if i == 0 else OrderStatus.FILLED))
    assert reasons(one) == []
    one.save_order(order("o10", OrderStatus.UNFILLED))
    assert reasons(one) == []  # the window is the last ten: o1..o10, one failure
    one.save_order(order("o11", OrderStatus.REJECTED))
    assert names(reasons(one)) == ["fill failures"]


def holding_store() -> SqliteStore:
    """An open YES position in HOLD from an episode that cited the minutes."""
    store = SqliteStore(MEMORY)
    store.save_episode(episode("ep-1", passage_ids=[f"{MINUTES.doc_id}#0"]))
    f = fill(Side.YES, 10, "0.62", "0.17", NOW - timedelta(days=1), fill_id="f1")
    store.save_order(order("o1", OrderStatus.FILLED, fills=[f]))
    store.remember_document(MINUTES.doc_id, MINUTES.content_hash, NOW - timedelta(days=1))
    return store


def test_unchanged_source_behind_an_open_position_is_fine() -> None:
    assert reasons(holding_store()) == []


def test_changed_source_alone() -> None:
    edited = MINUTES.model_copy(update={"text": "Revised text.", "content_hash": "different"})
    evidence = RecordedEvidence(world_file(documents=[edited]))
    assert names(reasons(holding_store(), evidence=evidence)) == ["source changed"]


def test_withdrawn_source_alone() -> None:
    evidence = RecordedEvidence(world_file(documents=[]))
    assert names(reasons(holding_store(), evidence=evidence)) == ["source withdrawn"]


def test_a_changed_source_behind_a_settled_position_does_not_halt() -> None:
    store = holding_store()
    store.record_settlement(HOLD, Side.YES, NOW - timedelta(hours=1))
    evidence = RecordedEvidence(world_file(documents=[]))
    assert reasons(store, evidence=evidence) == []


def ledger(n: int, resolved_at: datetime) -> list[LedgerEntry]:
    return [
        LedgerEntry(
            entry_id=f"e-{resolved_at.isoformat()}-{i}",
            episode_id=f"ep-{i}",
            mode="shadow",
            event_ticker="EV",
            market_ticker=f"M{i}",
            category="fed_decision",
            question="q",
            as_of=resolved_at - timedelta(days=3),
            probability=0.9,
            market_price=0.5,
            outcome=False,
            resolved_at=resolved_at,
        )
        for i in range(n)
    ]


def test_calibration_error_above_threshold_for_two_weeks() -> None:
    store = SqliteStore(MEMORY)
    store.add_ledger_entries(ledger(30, NOW - timedelta(weeks=2, days=1)))
    assert names(reasons(store)) == ["calibration"]


def test_calibration_error_for_less_than_two_weeks_does_not_halt() -> None:
    store = SqliteStore(MEMORY)
    store.add_ledger_entries(ledger(30, NOW - timedelta(days=10)))
    assert reasons(store) == []


def test_calibration_needs_enough_resolved_forecasts() -> None:
    store = SqliteStore(MEMORY)
    store.add_ledger_entries(ledger(29, NOW - timedelta(weeks=3)))
    assert reasons(store) == []


def test_enforce_halts_when_a_condition_holds() -> None:
    store = SqliteStore(MEMORY)
    executor, _, _ = paper()
    assert enforce(store, executor, CONFIG, SimClock(NOW), EVIDENCE) == []
    assert not store.get_halt().halted
    store.record_equity(NOW - timedelta(days=1), Decimal("1000"))
    store.record_equity(NOW - timedelta(hours=2), Decimal("960"))
    found = enforce(store, executor, CONFIG, SimClock(NOW), EVIDENCE)
    assert names(found) == ["daily loss stop"]
    state = store.get_halt()
    assert state.halted and state.reason.startswith("daily loss stop") and state.at == NOW
