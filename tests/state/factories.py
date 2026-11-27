"""Small record builders and fake ports for the state tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pmfa.records.base import RunMode
from pmfa.records.decision import (
    Fill,
    OrderResult,
    OrderStatus,
    Proposal,
    ReviewCard,
    ReviewCardPassage,
)
from pmfa.records.episode import EpisodeRecord, LedgerEntry, TraceStep
from pmfa.records.evidence import (
    EventHit,
    LedgerHit,
    Origin,
    Passage,
    RetrievedPassage,
    Trigger,
    TriggerKind,
)
from pmfa.records.forecast import Forecast, NodeStatus, TreeNode
from pmfa.records.market import Event, Market, MarketStatus, Side

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
EVENT = "KXFEDDECISION-26DEC"
MARKET = "KXFEDDECISION-26DEC-H0"


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def episode(episode_id: str = "ep-1", **overrides: Any) -> EpisodeRecord:
    fields: dict[str, Any] = {
        "episode_id": episode_id,
        "mode": RunMode.REPLAY,
        "trigger": Trigger(trigger_id="tr-1", kind=TriggerKind.SCHEDULE, at=T0, event_ticker=EVENT),
        "as_of": T0,
        "event_ticker": EVENT,
        "started_at": T0,
    }
    fields.update(overrides)
    return EpisodeRecord(**fields)


def step(seq: int, node: str = "match", kind: str = "action") -> TraceStep:
    return TraceStep(
        seq=seq, node=node, kind=kind, summary=f"step {seq}", data={"n": seq}, at=at(seq / 60)
    )


def node(
    node_id: str,
    *,
    depth: int = 1,
    status: NodeStatus = NodeStatus.SCORED,
    mass: float | None = None,
    episode_id: str = "ep-1",
    parent_id: str | None = None,
) -> TreeNode:
    return TreeNode(
        node_id=node_id,
        episode_id=episode_id,
        parent_id=parent_id,
        depth=depth,
        scheduled_event_id="fomc-2026-12",
        claim=f"claim {node_id}",
        status=status,
        path_mass=mass,
        outcome_probabilities={"hold": 0.6, "cut25": 0.4},
    )


def forecast(
    forecast_id: str = "fc-1", *, as_of: datetime = T0, system: str = "pipeline"
) -> Forecast:
    return Forecast(
        forecast_id=forecast_id,
        episode_id="ep-1",
        event_ticker=EVENT,
        as_of=as_of,
        system=system,
        outcome_probabilities={"hold": 0.6, "cut25": 0.4},
        market_probabilities={MARKET: 0.6},
        coverage=0.9,
        confidence=0.5,
        n_independent_origins=2,
    )


def proposal(proposal_id: str = "pr-1") -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        episode_id="ep-1",
        event_ticker=EVENT,
        market_ticker=MARKET,
        side=Side.NO,
        contracts=5,
        limit_price=Decimal("0.42"),
        edge_bound_price=Decimal("0.45"),
        reference_price=Decimal("0.41"),
        market_mid=Decimal("0.585"),
        forecast_probability=0.5,
        shrunk_probability=0.46,
        edge=Decimal("0.03"),
        fee=Decimal("0.09"),
        cost=Decimal("2.19"),
        confidence=0.5,
        position_before=0,
        position_target=-5,
        bankroll=Decimal("1000"),
        created_at=T0,
        expires_at=at(1 / 6),
    )


def card(proposal_id: str = "pr-1") -> ReviewCard:
    return ReviewCard(
        proposal_id=proposal_id,
        episode_id="ep-1",
        event_title="Fed decision in December",
        market_title="Hold",
        market_ticker=MARKET,
        side=Side.NO,
        contracts=5,
        limit_price=Decimal("0.42"),
        cost=Decimal("2.19"),
        forecast_probability=0.5,
        market_price=Decimal("0.585"),
        edge=Decimal("0.03"),
        confidence=0.5,
        routing_reasons=["cost_over_risk_unit"],
        passages=[
            ReviewCardPassage(
                passage_id="p-1",
                source="federalreserve.gov",
                title="Minutes",
                published_at=T0,
                excerpt="...",
            )
        ],
        expires_at=at(1 / 6),
    )


def order(order_id: str = "o-1", *, episode_id: str = "ep-1", fills: int = 2) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        client_order_id=f"c-{order_id}",
        episode_id=episode_id,
        market_ticker=MARKET,
        side=Side.NO,
        requested_contracts=5,
        limit_price=Decimal("0.42"),
        status=OrderStatus.PARTIALLY_FILLED if fills else OrderStatus.UNFILLED,
        fills=[
            Fill(
                fill_id=f"{order_id}-f{i}",
                order_id=order_id,
                market_ticker=MARKET,
                side=Side.NO,
                contracts=Decimal("2"),
                price=Decimal("0.41"),
                fee=Decimal("0.04"),
                filled_at=at(i),
                simulated=True,
            )
            for i in range(fills)
        ],
        placed_at=T0,
    )


def ledger_entry(
    entry_id: str,
    probability: float,
    *,
    market_price: float | None = 0.5,
    market: str = MARKET,
    category: str = "fed",
    system: str = "pipeline",
    outcome: bool | None = None,
    resolved_at: datetime | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        episode_id="ep-1",
        mode=RunMode.BACKTEST,
        system=system,
        event_ticker=EVENT,
        market_ticker=market,
        category=category,
        question=f"Will {market} resolve YES?",
        as_of=T0,
        probability=probability,
        market_price=market_price,
        outcome=outcome,
        resolved_at=resolved_at,
    )


def passage(passage_id: str = "p-1") -> Passage:
    return Passage(
        passage_id=passage_id,
        doc_id="d-1",
        origin_id="d-1",
        source="federalreserve.gov",
        url="https://www.federalreserve.gov/x",
        title="FOMC minutes",
        published_at=T0 - timedelta(days=1),
        index=0,
        text="Participants judged ...",
    )


class FakeKnowledge:
    """Records every call's arguments and returns one fixed answer per index."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def search_events(self, query: str, *, as_of: datetime, k: int = 5) -> list[EventHit]:
        self.calls.append(("search_events", {"query": query, "as_of": as_of, "k": k}))
        return [EventHit(event_ticker=EVENT, similarity=0.8)]

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        self.calls.append(
            (
                "search_evidence",
                {
                    "queries": queries,
                    "as_of": as_of,
                    "category": category,
                    "k": k,
                    "published_after": published_after,
                },
            )
        )
        return [
            RetrievedPassage(
                passage=passage(), similarity=0.7, recency=0.9, score=0.63, query=queries[0]
            )
        ]

    def search_ledger(
        self, query: str, *, as_of: datetime, category: str, k: int = 5
    ) -> list[LedgerHit]:
        self.calls.append(
            ("search_ledger", {"query": query, "as_of": as_of, "category": category, "k": k})
        )
        return [
            LedgerHit(
                question="Will the Fed hold in October?",
                category=category,
                probability=0.7,
                market_price=0.65,
                outcome=True,
                resolved_at=T0 - timedelta(days=5),
                similarity=0.5,
            )
        ]

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        self.calls.append(("get_passages", {"passage_ids": passage_ids, "as_of": as_of}))
        return [passage(pid) for pid in passage_ids if pid != "p-future"]

    def origins(self, passages: list[Passage], *, category: str) -> list[Origin]:
        raise AssertionError("the state service never resolves origins")


class FakeExchange:
    """Serves one event with two markets; everything else is not needed by the service."""

    def get_event(self, event_ticker: str) -> Event:
        from pmfa.errors import NotFound

        if event_ticker != EVENT:
            raise NotFound(f"no event {event_ticker!r}")
        markets = [
            Market(
                ticker=f"{EVENT}-{suffix}",
                event_ticker=EVENT,
                title=title,
                subtitle=title,
                status=MarketStatus.OPEN,
                open_time=T0 - timedelta(days=30),
                close_time=T0 + timedelta(days=37),
                rules_primary=f"If the Fed {title.lower()}, resolves Yes.",
                yes_bid=Decimal("0.55"),
                yes_ask=Decimal("0.57"),
            )
            for suffix, title in (("H0", "Holds"), ("C25", "Cuts 25bp"))
        ]
        return Event(
            event_ticker=EVENT,
            series_ticker="KXFEDDECISION",
            title="Fed decision in December 2026",
            category="Economics",
            mutually_exclusive=True,
            markets=markets,
        )
