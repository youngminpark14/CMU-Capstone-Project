"""The stage manager: advance only at the benchmarks, and demotion after two daily stops."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pmfa.execution.stages import StageManager, StageNotMet
from pmfa.records.base import Stage
from pmfa.records.decision import OrderStatus, Review, ReviewDecision, Routing, RoutingTier
from pmfa.records.episode import LedgerEntry
from pmfa.records.market import Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.clocks import SimClock
from tests.world.recorded.factories import HOLD

from .factories import CONFIG, episode, fill, order

T0 = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)


def manager(
    stage: Stage = Stage.BACKTEST, *, now: datetime = T0
) -> tuple[StageManager, SqliteStore, SimClock]:
    store = SqliteStore(MEMORY)
    clock = SimClock(now)
    if stage is not Stage.BACKTEST:
        store.set_stage(stage, "setup", T0)
    return StageManager(store, CONFIG, clock), store, clock


# -- backtest -> shadow


def backtest_entries(n: int, probability: float) -> list[LedgerEntry]:
    return [
        LedgerEntry(
            entry_id=f"e{i}",
            episode_id=f"ep{i}",
            mode="backtest",
            event_ticker=f"EV{i // 5}",
            market_ticker=f"M{i}",
            category="fed_decision",
            question="q",
            as_of=T0 - timedelta(days=30),
            probability=probability,
            market_price=0.6,
            outcome=True,
            resolved_at=T0 - timedelta(days=20),
            brier=(1 - probability) ** 2,
            market_brier=0.16,
        )
        for i in range(n)
    ]


def test_backtest_to_shadow_refused_below_200_markets() -> None:
    m, store, _ = manager()
    store.add_ledger_entries(backtest_entries(199, 0.8))
    status = m.status()
    assert (status.stage, status.next_stage, status.met) == (Stage.BACKTEST, Stage.SHADOW, False)
    assert status.benchmark.n_markets == 199
    with pytest.raises(StageNotMet, match="199 backtested markets"):
        m.advance()
    assert store.get_stage().stage is Stage.BACKTEST


def test_backtest_to_shadow_refused_without_skill() -> None:
    m, store, _ = manager()
    store.add_ledger_entries(backtest_entries(200, 0.5))
    with pytest.raises(StageNotMet, match="not above zero"):
        m.advance()


def test_backtest_to_shadow_allowed_at_200_markets_with_skill() -> None:
    m, store, _ = manager()
    store.add_ledger_entries(backtest_entries(200, 0.8))
    assert m.status().benchmark.skill > 0
    assert m.advance().stage is Stage.SHADOW


# -- shadow -> approval


def shadow_store(store: SqliteStore, *, won: bool = True) -> None:
    store.save_episode(
        episode("sh-1", mode="shadow", started_at=T0, routing=Routing(tier=RoutingTier.APPROVE))
    )
    f = fill(Side.YES, 10, "0.40", "0.17", T0 + timedelta(hours=1), fill_id="sf1")
    store.save_order(order("so1", OrderStatus.FILLED, episode_id="sh-1", fills=[f]))
    store.record_settlement(HOLD, Side.YES if won else Side.NO, T0 + timedelta(days=7))
    # A backtest episode's fill does not count toward shadow.
    store.save_episode(episode("bt-1", mode="backtest", started_at=T0 - timedelta(weeks=8)))
    g = fill(Side.NO, 10, "0.60", "0.17", T0 - timedelta(weeks=8), fill_id="bf1", market="X")
    store.save_order(order("bo1", OrderStatus.FILLED, episode_id="bt-1", fills=[g], market="X"))


def test_shadow_to_approval_refused_before_four_weeks() -> None:
    m, store, _ = manager(Stage.SHADOW, now=T0 + timedelta(weeks=4) - timedelta(minutes=1))
    shadow_store(store)
    status = m.status()
    assert status.benchmark.n_trades == 1
    assert status.benchmark.total_pnl == Decimal("5.83")
    with pytest.raises(StageNotMet, match="needs 4 weeks"):
        m.advance()


def test_shadow_to_approval_refused_on_a_loss() -> None:
    m, store, _ = manager(Stage.SHADOW, now=T0 + timedelta(weeks=5))
    shadow_store(store, won=False)
    with pytest.raises(StageNotMet, match="not positive"):
        m.advance()


def test_shadow_to_approval_allowed_at_four_weeks() -> None:
    m, store, _ = manager(Stage.SHADOW, now=T0 + timedelta(weeks=4))
    shadow_store(store)
    assert m.status().met
    assert m.advance().stage is Stage.APPROVAL


# -- approval -> autonomous


def reviews(store: SqliteStore, n: int, overridden: int, *, at: datetime) -> None:
    for i in range(n):
        decision = ReviewDecision.REJECT if i < overridden else ReviewDecision.APPROVE
        store.save_review(
            Review(
                proposal_id=f"p{i}",
                decision=decision,
                reason_code="other" if decision is ReviewDecision.REJECT else None,
                decided_at=at,
            )
        )


def test_approval_to_autonomous_thresholds() -> None:
    m, store, _ = manager(Stage.APPROVAL, now=T0 + timedelta(weeks=10))
    reviews(store, 50, 0, at=T0 - timedelta(days=1))  # before the stage: not counted
    reviews(store, 99, 14, at=T0 + timedelta(days=1))
    assert m.status().benchmark.n_trades == 99
    with pytest.raises(StageNotMet, match="99 approved trades"):
        m.advance()
    reviews(store, 1, 1, at=T0 + timedelta(days=2))  # 100 trades, 15 overridden
    with pytest.raises(StageNotMet, match="15 of 100"):
        m.advance()


def test_approval_to_autonomous_allowed_under_15_percent() -> None:
    m, store, _ = manager(Stage.APPROVAL, now=T0 + timedelta(weeks=10))
    reviews(store, 100, 14, at=T0 + timedelta(days=1))
    assert m.advance().stage is Stage.AUTONOMOUS
    status = m.status()
    assert status.next_stage is None and status.benchmark is None
    with pytest.raises(StageNotMet, match="top stage"):
        m.advance()


# -- demotion

NOW = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)  # Wednesday 15:00 in New York


def stop_day(store: SqliteStore, day: datetime) -> None:
    """A trading day that starts at 1000 and ends 4% down: the daily stop trips."""
    store.record_equity(day - timedelta(hours=6), Decimal("1000"))
    store.record_equity(day, Decimal("960"))
    store.record_equity(day + timedelta(minutes=30), Decimal("1000"))


def test_one_stop_in_a_week_does_not_demote() -> None:
    m, store, _ = manager(Stage.APPROVAL, now=NOW)
    stop_day(store, NOW - timedelta(days=2))
    assert m.check_demotion() is None
    assert store.get_stage().stage is Stage.APPROVAL


def test_two_stops_in_a_week_demote_one_stage_once() -> None:
    m, store, clock = manager(Stage.APPROVAL, now=NOW)
    stop_day(store, NOW - timedelta(days=5))
    stop_day(store, NOW - timedelta(days=1))
    state = m.check_demotion()
    assert state is not None and state.stage is Stage.SHADOW
    assert state.reason.startswith("demoted: daily loss stop tripped on 2 days")
    clock.advance(60)
    assert m.check_demotion() is None  # the same stops are not counted twice
    assert store.get_stage().stage is Stage.SHADOW


def test_two_stops_more_than_a_week_apart_do_not_demote() -> None:
    m, store, _ = manager(Stage.APPROVAL, now=NOW)
    stop_day(store, NOW - timedelta(days=9))
    stop_day(store, NOW - timedelta(days=1))
    assert m.check_demotion() is None


def test_demote_at_the_bottom_stays_at_backtest() -> None:
    m, _, _ = manager(now=NOW)
    assert m.demote("manual").stage is Stage.BACKTEST
