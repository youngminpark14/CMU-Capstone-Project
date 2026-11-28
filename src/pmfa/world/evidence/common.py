"""Small helpers shared by the Federal Reserve, BLS and calendar modules."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from pmfa.records.evidence import Document

EASTERN = ZoneInfo("America/New_York")
"""Both agencies publish on Eastern wall-clock time; the zone handles daylight saving."""

FED_SOURCE = "federalreserve.gov"
BLS_SOURCE = "bls.gov"

STATEMENT_RELEASE = time(14, 0)
"""FOMC statements and minutes are released at 2:00 p.m. Eastern (the Fed's calendar page says so)."""


def eastern_to_utc(day: date, at: time) -> datetime:
    """The UTC instant of an Eastern wall-clock time on ``day`` (daylight saving applied)."""
    return datetime.combine(day, at, tzinfo=EASTERN).astimezone(UTC)


def end_of_day_utc(day: date) -> datetime:
    """The last second of ``day`` in Eastern time, as UTC. Used when a source gives no time at all."""
    return eastern_to_utc(day, time(23, 59, 59))


def to_utc(moment: datetime) -> datetime:
    """Normalize to UTC; a naive time is a programming error, as everywhere in this project."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime: evidence times are timezone-aware")
    return moment.astimezone(UTC)


def content_hash(text: str) -> str:
    """SHA-256 of the document text. A change behind an open position is a halt condition."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_document(
    *,
    doc_id: str,
    source: str,
    kind: str,
    url: str,
    title: str,
    published_at: datetime,
    text: str,
) -> Document:
    """A :class:`Document` with its content hash filled in."""
    return Document(
        doc_id=doc_id,
        source=source,
        kind=kind,
        url=url,
        title=title,
        published_at=published_at,
        text=text,
        content_hash=content_hash(text),
    )


@dataclass
class FetchReport:
    """What a fetch of a window produced: the documents, and what could not be fetched."""

    documents: list[Document] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    """One line per page that failed or was refused, for the manifest and the report."""
