"""The outcome resolver on a paper account: what it settles, including a position traded back to flat."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pmfa.config import Config
from pmfa.evaluation.pnl import pnl_summary
from pmfa.evaluation.resolver import resolve_outcomes
from pmfa.evaluation.rows import TradeRow
from pmfa.records.decision import OrderIntent, RoutingTier
from pmfa.records.market import Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.backtest import BacktestData, build_backtest_world
from pmfa.world.clocks import SimClock
from tests.evaluation.backtest.support import FED, scripted


def _buy(world, order_id: str, side: Side, contracts: int):  # type: ignore[no-untyped-def]
    return world.executor.place(
        OrderIntent(
            client_order_id=order_id,
            episode_id="ep",
            proposal_id="p",
            market_ticker=f"{FED}-H0",
            side=side,
            contracts=contracts,
            limit_price=Decimal("0.99"),
        )
    )


def test_a_position_netted_to_flat_is_still_settled(data_config: Config) -> None:
    data = BacktestData(data_config)
    clock = SimClock(datetime(2026, 8, 13, 12, tzinfo=UTC))
    store = SqliteStore(MEMORY)
    world = build_backtest_world(
        data, data_config, clock=clock, store=store, models=scripted(data_config)
    )
    assert _buy(world, "a", Side.YES, 5).fills
    clock.set(datetime(2026, 8, 13, 12, 10, tzinfo=UTC))
    assert _buy(world, "b", Side.NO, 5).fills
    assert world.executor.positions() == []  # YES and NO net to flat

    clock.set(datetime(2026, 8, 25, tzinfo=UTC))
    report = resolve_outcomes(world)
    assert report.markets_settled == [f"{FED}-H0"]
    settled = {t: (r, at) for t, r, at in store.list_settlements()}
    assert f"{FED}-H0" in settled

    # The trade rows the runner builds now carry the result, so P&L matches the account.
    rows = [
        TradeRow(
            episode_id="ep",
            event_ticker=FED,
            market_ticker=f.market_ticker,
            side=f.side,
            contracts=f.contracts,
            price=f.price,
            fee=f.fee,
            tier=RoutingTier.APPROVE,
            filled_at=f.filled_at,
            result=settled[f.market_ticker][0],
            settled_at=settled[f.market_ticker][1],
        )
        for f in store.list_fills()
    ]
    summary = pnl_summary(rows)
    account = world.executor.account()  # type: ignore[attr-defined]
    assert summary.open_cost == 0
    assert summary.total == account.cash - world.executor.bankroll  # type: ignore[attr-defined]
    assert summary.total < 0  # a round trip costs the spread and two fees
    assert resolve_outcomes(world).markets_settled == []  # idempotent
    world.knowledge.close()  # type: ignore[attr-defined]
    data.close()
