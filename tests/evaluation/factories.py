"""Small builders for evaluation rows, so each test states only what it is about."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pmfa.evaluation.rows import ForecastRow, TradeRow
from pmfa.records.decision import RoutingTier
from pmfa.records.market import Side

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def frow(
    probability: float | None,
    market_price: float,
    outcome: bool,
    *,
    event: str = "EV-1",
    market: str | None = None,
    system: str = "pipeline",
    horizon_days: int = 1,
) -> ForecastRow:
    return ForecastRow(
        run_id="run-1",
        system=system,
        event_ticker=event,
        market_ticker=market or f"{event}-M",
        category="cpi",
        horizon_days=horizon_days,
        as_of=T0,
        probability=probability,
        market_price=market_price,
        outcome=outcome,
    )


def trade(
    side: Side,
    contracts: str,
    price: str,
    fee: str,
    *,
    result: Side | None,
    market: str = "EV-1-M",
    event: str = "EV-1",
    filled_hours: float = 0,
    settled_hours: float | None = None,
    episode: str = "ep-1",
) -> TradeRow:
    settled_at = None
    if result is not None:
        settled_at = T0 + timedelta(hours=settled_hours if settled_hours is not None else 24)
    return TradeRow(
        episode_id=episode,
        event_ticker=event,
        market_ticker=market,
        side=side,
        contracts=Decimal(contracts),
        price=Decimal(price),
        fee=Decimal(fee),
        tier=RoutingTier.APPROVE,
        filled_at=T0 + timedelta(hours=filled_hours),
        result=result,
        settled_at=settled_at,
    )
