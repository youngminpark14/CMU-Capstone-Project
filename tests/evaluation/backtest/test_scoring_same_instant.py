"""The scoring pass keeps fixed as-of times, so two of its episodes can share one instant.

Two scheduled episodes for the Fed event run on one backtest world at the same
clock time, as two scoring points with the same as-of time would. The first
buys the hold market, priced from the one-minute candle ending two minutes
later. The second decides at the same as-of time, so it must read no position,
balance, equity point or order outcome from the first order.

The store starts with nine unfilled orders from an earlier day, one short of the
fill-failure window. Counted at the shared instant, the first episode's order
would make ten and halt the second episode; its outcome is not known until the
candle ends, so it is not counted then.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from pmfa.config import Config
from pmfa.decision.risk import risk_state
from pmfa.execution.halt import stop_reasons
from pmfa.records.decision import OrderResult, OrderStatus
from pmfa.records.episode import Exit
from pmfa.records.evidence import Trigger, TriggerKind
from pmfa.records.market import Side
from pmfa.state.store import SqliteStore
from pmfa.world.backtest import BacktestData, build_backtest_world
from pmfa.world.clocks import SimClock
from tests.evaluation.backtest.support import CPI, CPI_CLOSE, DOC1_AT, FED, RESULTS, scripted

CPI_3D = CPI_CLOSE - timedelta(days=3)


def _trigger(name: str) -> Trigger:
    return Trigger(
        trigger_id=f"schedule:{FED}:{name}", kind=TriggerKind.SCHEDULE, at=DOC1_AT, event_ticker=FED
    )


def _unfilled(i: int) -> OrderResult:
    """An unfilled order a day before, from an episode that is not in the store."""
    return OrderResult(
        order_id=f"earlier-{i}",
        client_order_id=f"earlier-{i}",
        episode_id="earlier",
        market_ticker=f"{FED}-EARLIER",
        side=Side.YES,
        requested_contracts=1,
        limit_price="0.50",
        status=OrderStatus.UNFILLED,
        placed_at=DOC1_AT - timedelta(days=1),
    )


def test_a_second_decision_at_the_same_instant_reads_nothing_of_the_first_order(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.episode import EpisodeController

    data = BacktestData(data_config)
    clock = SimClock(DOC1_AT)
    store = SqliteStore(tmp_path / "scoring.sqlite")
    world = build_backtest_world(
        data,
        data_config,
        clock=clock,
        store=store,
        models=scripted(data_config),
        knowledge=data.guard(data.knowledge(tmp_path / "index")),
    )
    bankroll = world.config.execution.notional_bankroll
    world.executor.mark(DOC1_AT - timedelta(days=1))  # type: ignore[attr-defined]
    window = world.config.oversight.fill_failure_window
    for i in range(window - 1):
        store.save_order(_unfilled(i))

    controller = EpisodeController(world)
    try:
        first = controller.run(_trigger("a"))
        second = controller.run(_trigger("b"))
    finally:
        controller.close()

    assert first.exit is Exit.EXECUTED
    [order] = store.list_orders(episode_id=first.episode_id)
    assert order.fills and order.outcome_at == DOC1_AT + timedelta(minutes=2)
    # The second episode decided at the same as-of time, on an account without the first order.
    assert second.as_of == clock.now() == DOC1_AT
    assert second.exit is not Exit.HALTED
    assert second.proposal is not None
    assert second.proposal.position_before == 0
    assert second.proposal.bankroll == bankroll
    # Read at that time, the ports show no position, balance, equity or outcome of it.
    assert world.executor.positions() == []
    assert world.executor.balance().equity == bankroll
    assert risk_state(store.equity_series(), DOC1_AT, world.config).bankroll == bankroll
    assert stop_reasons(store, world.config, clock, world.evidence) == []
    # Once the first order's candle has ended, its outcome counts: ten orders, enough to judge.
    clock.set(order.outcome_at)
    assert any(
        r.startswith("fill failures")
        for r in stop_reasons(store, world.config, clock, world.evidence)
    )
    store.close()
    data.close()


def test_a_resolve_at_a_shared_instant_settles_only_what_is_known_then(
    data_config: Config, tmp_path: Path
) -> None:
    """The scoring pass resolves before each point, and a point is never delayed.

    A Fed order from Aug 10 has settled by the CPI three-day point; a CPI order
    placed at that point has its outcome at the end of a later candle. The
    resolver runs at that same instant, as it would before a second point with
    the same as-of time: it settles the Fed market and leaves the CPI order
    unresolved, and the next decision at that instant reads the Fed result only.
    """
    from pmfa.episode import EpisodeController
    from pmfa.evaluation.resolver import resolve_outcomes

    data = BacktestData(data_config)
    clock = SimClock(DOC1_AT)
    store = SqliteStore(tmp_path / "scoring.sqlite")
    world = build_backtest_world(
        data,
        data_config,
        clock=clock,
        store=store,
        models=scripted(data_config),
        knowledge=data.guard(data.knowledge(tmp_path / "index")),
    )
    bankroll = world.config.execution.notional_bankroll
    world.executor.mark(DOC1_AT - timedelta(days=1))  # type: ignore[attr-defined]
    controller = EpisodeController(world)
    try:
        assert controller.run(_trigger("fed")).exit is Exit.EXECUTED
        clock.set(CPI_3D)
        first = controller.run(_cpi_trigger("a"))
        assert first.exit is Exit.EXECUTED
        [order] = store.list_orders(episode_id=first.episode_id)
        assert order.fills and order.outcome_at is not None and order.outcome_at > CPI_3D

        report = resolve_outcomes(world, now=clock.now())
        fed_fills = [f for f in store.list_fills() if f.market_ticker.startswith(FED)]
        fed_markets = sorted({f.market_ticker for f in fed_fills})
        assert fed_fills and report.markets_settled == fed_markets
        assert order.market_ticker in report.unresolved
        settlements = store.list_settlements()
        assert {t for t, _, _ in settlements} == set(fed_markets)
        assert all(at <= clock.now() for _, _, at in settlements)
        assert all(e.outcome is None for e in store.ledger() if e.market_ticker.startswith(CPI))

        # Known at this instant: the Fed result, not the CPI order.
        cost = sum((f.price * f.contracts + f.fee for f in fed_fills), Decimal(0))
        payout = sum(
            (f.contracts for f in fed_fills if f.side is RESULTS[f.market_ticker]), Decimal(0)
        )
        known = bankroll - cost + payout
        assert known > bankroll  # the Fed order won, so this is the peak
        assert world.executor.positions() == []
        assert world.executor.balance().equity == known

        second = controller.run(_cpi_trigger("b"))
    finally:
        controller.close()
    assert second.as_of == CPI_3D
    assert second.proposal is not None
    assert second.proposal.position_before == 0
    assert second.proposal.bankroll == known
    store.close()
    data.close()


def _cpi_trigger(name: str) -> Trigger:
    return Trigger(
        trigger_id=f"schedule:{CPI}:{name}", kind=TriggerKind.SCHEDULE, at=CPI_3D, event_ticker=CPI
    )
