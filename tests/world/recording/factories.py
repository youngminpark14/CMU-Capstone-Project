"""A small hand-built world for the recording tests, and scripted roles that trade in it.

One December Fed-decision event (five markets) plus an unrelated January event,
deep one-level books, and four documents: the trigger's minutes, a speech, an
unrelated release, and a canary dated after the trigger. The scripted roles
match the December event, keep every passage, propose a hold and a cut
scenario, and score them hawkishly, so the episode buys NO on the cut market.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pmfa.config import Config
from pmfa.models import ScriptedModels
from pmfa.records.base import Record
from pmfa.records.evidence import Document, ScheduledEvent, Trigger, TriggerKind
from pmfa.records.market import BookLevel, Event, Market, MarketStatus, OrderBook, Series
from pmfa.records.roles import (
    GenerateInput,
    GenerateOutput,
    MatchDecision,
    OptionProbability,
    PassageSelection,
    PassageSelectionInput,
    QueryPlan,
    RoleTask,
    ScenarioScore,
    ScenarioThought,
    ScoreInput,
    ScoreOutput,
)
from pmfa.world.evidence.common import content_hash
from pmfa.world.recorded import RecordedWorldFile

AS_OF = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
DEC = "KXFEDDECISION-26DEC"
JAN = "KXFEDDECISION-27JAN"
CUT = f"{DEC}-C25"
OPTIONS = {
    "H26": ("Hike", ">25", "Hike >25bps", ("0.01", "0.03")),
    "H25": ("Hike", "25", "Hike 25bps", ("0.02", "0.04")),
    "H0": ("Hike", "0", "Fed maintains rate", ("0.61", "0.63")),
    "C25": ("Cut", "25", "Cut 25bps", ("0.32", "0.34")),
    "C26": ("Cut", ">25", "Cut >25bps", ("0.01", "0.03")),
}
HAWKISH = {"hike_gt25": 0.01, "hike_25": 0.06, "hold": 0.70, "cut_25": 0.18, "cut_gt25": 0.05}


def series() -> Series:
    return Series(
        ticker="KXFEDDECISION",
        title="Fed decision",
        category="Economics",
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal("1"),
    )


def fed_event(ticker: str = DEC, close: datetime = datetime(2026, 12, 9, 18, 59, tzinfo=UTC)):
    markets = []
    for suffix, (direction, amount, label, (bid, ask)) in OPTIONS.items():
        markets.append(
            Market(
                ticker=f"{ticker}-{suffix}",
                event_ticker=ticker,
                title=f"Fed decision on {close:%b %d, %Y}: {label}",
                subtitle=label,
                status=MarketStatus.OPEN,
                open_time=datetime(2026, 9, 1, tzinfo=UTC),
                close_time=close,
                strike_type="custom",
                custom_strike={direction: amount},
                rules_primary=(
                    f"If the Federal Reserve does a {direction} of {amount}bps on "
                    f"{close:%B %d, %Y}, then the market resolves to Yes."
                ),
                yes_bid=bid,
                yes_ask=ask,
            )
        )
    return Event(
        event_ticker=ticker,
        series_ticker="KXFEDDECISION",
        title=f"Fed decision in {close:%b %Y}?",
        category="Economics",
        mutually_exclusive=True,
        markets=markets,
    )


def books(event: Event) -> dict[str, OrderBook]:
    """One deep level per side at each market's quote."""
    out = {}
    for m in event.markets:
        assert m.yes_bid is not None and m.yes_ask is not None
        out[m.ticker] = OrderBook(
            ticker=m.ticker,
            as_of=AS_OF,
            yes_bids=[BookLevel(price=m.yes_bid, contracts="1000")],
            no_bids=[BookLevel(price=Decimal(1) - m.yes_ask, contracts="1000")],
        )
    return out


def document(doc_id: str, text: str, published_at: datetime, source: str = "federalreserve.gov"):
    return Document(
        doc_id=doc_id,
        source=source,
        kind="fomc_minutes",
        url=f"https://{source}/{doc_id}.htm",
        title=f"Document {doc_id}",
        published_at=published_at,
        text=text,
        content_hash=content_hash(text),
    )


MINUTES = document(
    "minutes-2026-10",
    "Participants judged that inflation remained elevated and that the federal funds rate "
    "should stay restrictive. Many participants saw the case for holding the federal funds "
    "rate steady at the December meeting; few saw a rate cut in December as appropriate.",
    AS_OF - timedelta(minutes=30),
)
SPEECH = document(
    "speech-2026-11-12",
    "I would support holding the federal funds rate steady in December while inflation data "
    "settle, and I see little case for a rate cut at the December meeting.",
    AS_OF - timedelta(days=8),
)
UNRELATED = document(
    "release-unrelated",
    "Retail sales of garden furniture rose in the spring quarter, a trade association reported.",
    AS_OF - timedelta(days=3),
    source="trade.example",
)
CANARY = document(
    "canary-2026-11-21",
    "CANARY-7f3a91 The Committee held the federal funds rate steady in December; no rate cut.",
    AS_OF + timedelta(days=1),
)
FOMC_DEC = ScheduledEvent(
    scheduled_event_id="fomc:2026-12",
    kind="fomc_decision",
    title="FOMC decision, December 2026",
    scheduled_at=datetime(2026, 12, 9, 19, 0, tzinfo=UTC),
)
UNUSED_ENTRY = ScheduledEvent(
    scheduled_event_id="cpi:2027-01",
    kind="cpi_release",
    title="CPI for December 2026",
    scheduled_at=datetime(2027, 1, 13, 13, 30, tzinfo=UTC),
)


def world_file(**changes: Any) -> RecordedWorldFile:
    dec, jan = fed_event(), fed_event(JAN, datetime(2027, 1, 27, 18, 59, tzinfo=UTC))
    base: dict[str, Any] = {
        "description": "Hawkish minutes before the December meeting (recording test world).",
        "provenance": "hand-made fixture",
        "trigger": Trigger(
            trigger_id="trig-minutes",
            kind=TriggerKind.NEW_DOCUMENT,
            at=AS_OF,
            doc_id=MINUTES.doc_id,
        ),
        "series": [series()],
        "events": [dec, jan],
        "order_books": {**books(dec), **books(jan)},
        "documents": [MINUTES, SPEECH, UNRELATED, CANARY],
        "scheduled_events": [FOMC_DEC, UNUSED_ENTRY],
        "stage": "approval",
        "config_overrides": {"state": {"transport": "direct"}},
    }
    base.update(changes)
    return RecordedWorldFile.model_validate(base)


HOLD = "The Committee holds the rate in December"
CUTS = "The Committee cuts 25 bp in December"


def scripted(config: Config) -> ScriptedModels:
    """Every role answered from a table: match December, keep all, hold 0.7 / cut 0.3."""

    def select(payload: Record) -> Record:
        assert isinstance(payload, PassageSelectionInput)
        return PassageSelection(
            keep_passage_ids=[
                p.passage_id for p in payload.passages if "canary" not in p.passage_id
            ],
            dropped=[],
            rationale="Kept the passages about the December decision.",
        )

    def generate(payload: Record) -> Record:
        assert isinstance(payload, GenerateInput)
        cite = [p.passage_id for p in payload.passages[:1]]
        return GenerateOutput(
            thoughts=[
                ScenarioThought(
                    claim=claim,
                    outcome_ids=[outcome],
                    value_low=None,
                    value_high=None,
                    passage_ids=cite,
                    rationale=f"Generator on {claim}.",
                )
                for claim, outcome in ((HOLD, "hold"), (CUTS, "cut_25"))
            ]
        )

    def critic(payload: Record) -> Record:
        assert isinstance(payload, ScoreInput)
        return ScoreOutput(
            scores=[
                ScenarioScore(
                    scenario_id=s.scenario_id,
                    verdict="scored",
                    probability=0.7 if s.claim == HOLD else 0.3,
                    option_probabilities=[
                        OptionProbability(outcome_id=k, probability=v) for k, v in HAWKISH.items()
                    ],
                    quantiles=None,
                    rejection_reason="",
                    evidence_request="",
                    rationale=f"Critic on {s.claim}.",
                )
                for s in payload.scenarios
            ]
        )

    return ScriptedModels(
        {
            RoleTask.MATCH: lambda _: MatchDecision(
                matched=True, event_ticker=DEC, rationale="The minutes bear on December."
            ),
            RoleTask.PLAN_QUERIES: lambda _: QueryPlan(
                query_for="federal funds rate steady December hold inflation",
                query_against="rate cut December meeting",
                rationale="One query each way.",
            ),
            RoleTask.SELECT_PASSAGES: select,
            RoleTask.GENERATE: generate,
            RoleTask.SCORE: critic,
        },
        config=config,
    )
