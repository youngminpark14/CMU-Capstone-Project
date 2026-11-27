"""A small hand-made recorded world: one Fed-decision event with two markets.

Trigger-time book for the hold market (``H0``)::

    YES bids  0.60 x 50                 -> NO asks  0.40 x 50
    NO bids   0.38 x 30, 0.36 x 40      -> YES asks 0.62 x 30, 0.64 x 40

Documents: the trigger's minutes and a speech before the trigger, and a canary
dated one day after it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pmfa.records.base import Record
from pmfa.records.decision import Fill
from pmfa.records.episode import LedgerEntry, ModelResult, Usage
from pmfa.records.evidence import Document, ScheduledEvent, Trigger, TriggerKind
from pmfa.records.market import (
    BookLevel,
    Event,
    Market,
    MarketStatus,
    OrderBook,
    Series,
)
from pmfa.records.roles import ModelSlot, RoleTask
from pmfa.world.evidence.common import content_hash
from pmfa.world.recorded import RecordedWorldFile

AS_OF = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
SERIES = "KXFEDDECISION"
EVENT = "KXFEDDECISION-26DEC"
HOLD = f"{EVENT}-H0"
CUT = f"{EVENT}-C25"
CANARY = "CANARY-7f3a91 this document is dated after every forecast and must never be retrieved"


def series() -> Series:
    return Series(
        ticker=SERIES,
        title="Fed decision",
        category="fed_decision",
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal("1"),
    )


def market(ticker: str, subtitle: str, bid: str, ask: str, **extra: Any) -> Market:
    fields: dict[str, Any] = {
        "ticker": ticker,
        "event_ticker": EVENT,
        "title": f"Fed decision in December: {subtitle}",
        "subtitle": subtitle,
        "status": MarketStatus.OPEN,
        "open_time": datetime(2026, 10, 29, tzinfo=UTC),
        "close_time": datetime(2026, 12, 9, 18, 55, tzinfo=UTC),
        "rules_primary": "Resolves YES if the FOMC announces the stated change on December 9, 2026.",
        "yes_bid": bid,
        "yes_ask": ask,
        "yes_bid_size": "50",
        "yes_ask_size": "30",
    }
    return Market.model_validate({**fields, **extra})


def event(**extra: Any) -> Event:
    return Event(
        event_ticker=EVENT,
        series_ticker=SERIES,
        title="Fed decision in December 2026",
        category="fed_decision",
        mutually_exclusive=True,
        markets=[
            market(HOLD, "Fed maintains rate", "0.60", "0.62", **extra),
            market(CUT, "Cut 25bp", "0.36", "0.38"),
        ],
    )


def book(
    ticker: str = HOLD,
    yes_bids: tuple[tuple[str, str], ...] = (("0.60", "50"),),
    no_bids: tuple[tuple[str, str], ...] = (("0.38", "30"), ("0.36", "40")),
    at: datetime = AS_OF,
) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        as_of=at,
        yes_bids=[BookLevel(price=p, contracts=c) for p, c in yes_bids],
        no_bids=[BookLevel(price=p, contracts=c) for p, c in no_bids],
    )


def document(doc_id: str, title: str, text: str, published_at: datetime) -> Document:
    return Document(
        doc_id=doc_id,
        source="federalreserve.gov",
        kind="fomc_minutes",
        url=f"https://www.federalreserve.gov/{doc_id}.htm",
        title=title,
        published_at=published_at,
        text=text,
        content_hash=content_hash(text),
    )


MINUTES = document(
    "fomc-minutes-2026-10",
    "Minutes of the Federal Open Market Committee, October 2026",
    "Participants judged that inflation remained somewhat elevated. Many participants "
    "saw the case for holding the federal funds rate steady at the December meeting.",
    AS_OF - timedelta(hours=1),
)
SPEECH = document(
    "speech-2026-11-18",
    "Governor remarks on the outlook",
    "I would support holding the policy rate steady in December while inflation data settle.",
    AS_OF - timedelta(days=2),
)
FUTURE = document(
    "canary-2026-11-21",
    "Federal funds rate decision leak",
    f"{CANARY}. The Committee held the federal funds rate steady in December.",
    AS_OF + timedelta(days=1),
)


def prior_fill() -> Fill:
    return Fill(
        fill_id="prior-fill-1",
        order_id="prior-order-1",
        market_ticker=CUT,
        side="no",
        contracts="5",
        price="0.60",
        fee="0.09",
        filled_at=AS_OF - timedelta(days=3),
        simulated=True,
    )


def resolved_entry() -> LedgerEntry:
    return LedgerEntry(
        entry_id="ledger-oct-hold",
        episode_id="ep-oct",
        mode="backtest",
        event_ticker="KXFEDDECISION-26OCT",
        market_ticker="KXFEDDECISION-26OCT-H0",
        category="fed_decision",
        question="Will the Fed hold rates in October 2026?",
        as_of=datetime(2026, 10, 20, tzinfo=UTC),
        probability=0.7,
        market_price=0.65,
        outcome=True,
        resolved_at=datetime(2026, 10, 28, 18, tzinfo=UTC),
        brier=0.09,
        market_brier=0.1225,
    )


def world_file(**changes: Any) -> RecordedWorldFile:
    base: dict[str, Any] = {
        "description": "Hawkish October minutes before the December Fed meeting.",
        "provenance": "hand-made fixture",
        "trigger": Trigger(
            trigger_id="trig-minutes",
            kind=TriggerKind.NEW_DOCUMENT,
            at=AS_OF,
            doc_id=MINUTES.doc_id,
        ),
        "series": [series()],
        "events": [event()],
        "order_books": {HOLD: book()},
        "documents": [MINUTES, SPEECH, FUTURE],
        "scheduled_events": [
            ScheduledEvent(
                scheduled_event_id="fomc-2026-12",
                kind="resolution",
                title="FOMC decision, December 2026",
                scheduled_at=datetime(2026, 12, 9, 19, tzinfo=UTC),
            )
        ],
        "stage": "approval",
    }
    base.update(changes)
    return RecordedWorldFile.model_validate(base)


class NoModels:
    """A ``Models`` stand-in: these tests never call a model."""

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        raise AssertionError("no model calls in these tests")

    def usage(self, episode_id: str) -> Usage:
        return Usage()
