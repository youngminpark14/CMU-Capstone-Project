"""Evidence records: documents, passages, origins, scheduled events, triggers."""

from __future__ import annotations

from enum import StrEnum

from pmfa.records.base import Record, UtcTime


class Document(Record):
    """One published document from a followed source."""

    doc_id: str
    source: str
    """Publisher key, e.g. ``federalreserve.gov`` or ``bls.gov``."""
    kind: str
    """``fomc_statement``, ``fomc_minutes``, ``speech``, ``cpi_release``, ``employment_situation``, ``news``, ..."""
    url: str
    title: str
    published_at: UtcTime
    text: str
    content_hash: str = ""
    """Hash of ``text``; a change behind an open position is a halt condition."""


class Passage(Record):
    """A chunk of a document: about 250 words, tagged with where and when it was published."""

    passage_id: str
    doc_id: str
    origin_id: str
    """The original document this text traces back to after duplicates are merged."""
    source: str
    url: str
    title: str
    published_at: UtcTime
    index: int
    text: str


class Origin(Record):
    """A publisher's original document after duplicates are merged."""

    origin_id: str
    source: str
    url: str
    published_at: UtcTime
    is_primary: bool
    """True when ``source`` is an allowlisted official publisher for the event's category."""


class RetrievedPassage(Record):
    passage: Passage
    similarity: float
    recency: float
    """Recency decay factor in (0, 1]; 1 when ranking ignores recency."""
    score: float
    query: str = ""


class ScheduledEvent(Record):
    """An entry in the release calendar: a meeting decision or a data release."""

    scheduled_event_id: str
    kind: str
    """``fomc_decision``, ``cpi_release``, ``employment_situation``, or ``resolution``."""
    title: str
    scheduled_at: UtcTime
    period: str = ""
    """The reference period when there is one, e.g. ``2026-10`` for October CPI."""


class TriggerKind(StrEnum):
    NEW_DOCUMENT = "new_document"
    PRICE_MOVE = "price_move"
    SCHEDULE = "schedule"


class Trigger(Record):
    """What starts an episode. ``at`` becomes the episode's as-of time."""

    trigger_id: str
    kind: TriggerKind
    at: UtcTime
    doc_id: str | None = None
    """The new document, for ``new_document`` triggers."""
    event_ticker: str | None = None
    """The event, for ``price_move`` and ``schedule`` triggers (no matching needed)."""
    detail: str = ""


class EventHit(Record):
    """A candidate event from the events index."""

    event_ticker: str
    similarity: float


class LedgerHit(Record):
    """A resolved forecast from the ledger index, shown to the critic as a calibration example."""

    question: str
    category: str
    probability: float
    market_price: float
    outcome: bool
    resolved_at: UtcTime
    similarity: float
