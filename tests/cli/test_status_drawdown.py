"""``pmfa report-status`` measures drawdown from the bankroll the mode actually has.

In demo mode the bankroll is the demo subaccount's balance, which the demo
executor records as equity marks; elsewhere it is the configured notional
amount. A $200 loss is 2% of a $10,000 subaccount, not 20% of the notional
$1,000, so the report must start from the right base and say which it used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pmfa.cli.state import status_report
from pmfa.config import Config
from pmfa.records.base import RunMode
from pmfa.records.decision import Fill, OrderResult, OrderStatus
from pmfa.records.episode import EpisodeRecord
from pmfa.records.evidence import Trigger, TriggerKind
from pmfa.records.market import Side
from pmfa.state.store import SqliteStore

T0 = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
TICKER = "KXTEST-26OCT-A"


def store_with_a_200_dollar_loss(
    path: Path, mode: RunMode, first_mark: Decimal | None
) -> SqliteStore:
    """One episode in ``mode`` that bought 1000 YES at $0.20, no fee, and the market settled NO."""
    store = SqliteStore(path)
    trigger = Trigger(trigger_id="t-1", kind=TriggerKind.SCHEDULE, at=T0, event_ticker="KXTEST")
    store.save_episode(
        EpisodeRecord(
            episode_id="ep-1",
            mode=mode,
            trigger=trigger,
            as_of=T0,
            event_ticker="KXTEST-26OCT",
            started_at=T0,
        )
    )
    fill = Fill(
        fill_id="f-1",
        order_id="o-1",
        market_ticker=TICKER,
        side=Side.YES,
        contracts=Decimal("1000"),
        price=Decimal("0.20"),
        fee=Decimal("0"),
        filled_at=T0 + timedelta(minutes=1),
        simulated=mode is not RunMode.DEMO,
    )
    store.save_order(
        OrderResult(
            order_id="o-1",
            client_order_id="c-1",
            episode_id="ep-1",
            market_ticker=TICKER,
            side=Side.YES,
            requested_contracts=1000,
            limit_price=Decimal("0.20"),
            status=OrderStatus.FILLED,
            fills=[fill],
            placed_at=T0 + timedelta(minutes=1),
        )
    )
    store.record_settlement(TICKER, Side.NO, T0 + timedelta(days=1))
    if first_mark is not None:
        store.record_equity(T0, first_mark)
        store.record_equity(T0 + timedelta(days=1), first_mark - Decimal("200"))
    return store


def drawdown_line(lines: list[str]) -> str:
    found = [line for line in lines if "max drawdown" in line]
    assert len(found) == 1, lines
    return found[0]


def test_demo_drawdown_is_measured_from_the_subaccounts_first_equity_mark(tmp_path):
    store = store_with_a_200_dollar_loss(tmp_path / "demo.sqlite", RunMode.DEMO, Decimal("10000"))
    line = drawdown_line(status_report(store, Config(), RunMode.DEMO))
    assert "max drawdown 0.0200 ($200" in line
    assert "from a $10000 bankroll (the demo subaccount's first recorded equity)" in line


def test_shadow_drawdown_stays_on_the_notional_bankroll_even_with_equity_marks(tmp_path):
    store = store_with_a_200_dollar_loss(
        tmp_path / "shadow.sqlite", RunMode.SHADOW, Decimal("10000")
    )
    config = Config()
    line = drawdown_line(status_report(store, config, RunMode.SHADOW))
    assert config.execution.notional_bankroll == Decimal("1000")
    assert "max drawdown 0.2000 ($200" in line
    assert "from a $1000 bankroll (the notional bankroll)" in line


def test_demo_without_a_usable_mark_falls_back_to_the_notional_bankroll(tmp_path):
    for name, mark in [("none", None), ("zero", Decimal("0"))]:
        store = store_with_a_200_dollar_loss(tmp_path / f"{name}.sqlite", RunMode.DEMO, mark)
        line = drawdown_line(status_report(store, Config(), RunMode.DEMO))
        assert (
            "from a $1000 bankroll (the notional bankroll: no usable demo equity mark)" in line
        ), name
