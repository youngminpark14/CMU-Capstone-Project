"""Builders for execution tests: a store, a paper executor on the recorded fixture, proposals, fills."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pmfa.config import Config
from pmfa.execution.paper import PaperExecutor
from pmfa.records.decision import Fill, OrderIntent, OrderResult, OrderStatus, Proposal
from pmfa.records.episode import EpisodeRecord
from pmfa.records.evidence import Trigger, TriggerKind
from pmfa.records.market import Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.clocks import SimClock
from pmfa.world.recorded import RecordedBooks, RecordedExchange, RecordedWorldFile
from tests.world.recorded.factories import AS_OF, EVENT, HOLD, world_file

CONFIG = Config()


def paper(
    file: RecordedWorldFile | None = None, *, bankroll: str | None = None
) -> tuple[PaperExecutor, SqliteStore, SimClock]:
    file = file or world_file()
    clock = SimClock(AS_OF)
    store = SqliteStore(MEMORY)
    exchange = RecordedExchange(file, clock, CONFIG)
    executor = PaperExecutor(
        store,
        clock,
        CONFIG,
        exchange,
        RecordedBooks(file),
        bankroll=Decimal(bankroll) if bankroll is not None else None,
    )
    return executor, store, clock


def intent(
    contracts: int, limit: str, *, side: Side = Side.YES, client_order_id: str = "coid-1"
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        episode_id="ep-1",
        proposal_id="prop-1",
        market_ticker=HOLD,
        side=side,
        contracts=contracts,
        limit_price=limit,
    )


def fill(
    side: Side,
    contracts: int,
    price: str,
    fee: str,
    at: datetime,
    *,
    market: str = HOLD,
    fill_id: str | None = None,
) -> Fill:
    return Fill(
        fill_id=fill_id or f"f-{market}-{side}-{contracts}-{price}-{at.isoformat()}",
        order_id=f"o-{fill_id or at.isoformat()}",
        market_ticker=market,
        side=side,
        contracts=Decimal(contracts),
        price=price,
        fee=fee,
        filled_at=at,
        simulated=True,
    )


def order(
    order_id: str,
    status: OrderStatus,
    *,
    episode_id: str = "ep-1",
    fills: list[Fill] | None = None,
    at: datetime = AS_OF,
    market: str = HOLD,
) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        client_order_id=order_id,
        episode_id=episode_id,
        market_ticker=market,
        side=Side.YES,
        requested_contracts=10,
        limit_price="0.62",
        status=status,
        fills=fills or [],
        placed_at=at,
    )


def proposal(**changes: Any) -> Proposal:
    base: dict[str, Any] = {
        "proposal_id": "prop-1",
        "episode_id": "ep-1",
        "event_ticker": EVENT,
        "market_ticker": HOLD,
        "side": Side.YES,
        "contracts": 10,
        "limit_price": "0.62",
        "edge_bound_price": "0.64",
        "reference_price": "0.62",
        "market_mid": "0.61",
        "forecast_probability": 0.7,
        "shrunk_probability": 0.655,
        "edge": Decimal("0.0333"),
        "fee": "0.17",
        "cost": "6.37",
        "confidence": 0.8,
        "position_before": 0,
        "position_target": 10,
        "bankroll": "1000",
        "created_at": AS_OF,
        "expires_at": AS_OF + timedelta(minutes=10),
    }
    base.update(changes)
    return Proposal.model_validate(base)


def episode(
    episode_id: str, *, mode: str = "shadow", passage_ids: list[str] | None = None, **extra: Any
) -> EpisodeRecord:
    return EpisodeRecord.model_validate(
        {
            "episode_id": episode_id,
            "mode": mode,
            "trigger": Trigger(
                trigger_id=f"trig-{episode_id}", kind=TriggerKind.SCHEDULE, at=AS_OF
            ),
            "as_of": AS_OF,
            "event_ticker": EVENT,
            "passage_ids": passage_ids or [],
            "started_at": extra.pop("started_at", AS_OF),
            **extra,
        }
    )
