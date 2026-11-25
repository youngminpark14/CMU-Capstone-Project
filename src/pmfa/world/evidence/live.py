"""Live evidence for the shadow and demo modes.

:class:`LiveEvidence` implements the Evidence port by polling the listing
sources (the Federal Reserve's press-release and speech JSON, BLS releases) and
adding what is new to a :class:`~pmfa.world.evidence.corpus.CorpusStore`. Reads
then come from the store with the same as-of rule as the frozen corpus, so
shadow mode and a backtest filter documents the same way.

Politeness: a poll costs two listing requests to federalreserve.gov plus one
page per new document, spaced by the fetcher, and calls to ``documents`` poll at
most once per ``poll_interval_seconds`` of the clock. Pages already in the store
are not fetched again. If BLS refuses automated access, that is remembered by
the fetcher's cache and only manual files (see ``pmfa.world.evidence.bls``)
bring in BLS text.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pmfa.errors import NotFound
from pmfa.records.evidence import Document, ScheduledEvent
from pmfa.world.clocks import SystemClock
from pmfa.world.evidence.bls import BlsSource
from pmfa.world.evidence.calendar import ReleaseCalendar, build_calendar
from pmfa.world.evidence.common import to_utc
from pmfa.world.evidence.corpus import CorpusStore
from pmfa.world.evidence.fed import FedSource
from pmfa.world.evidence.http import Fetcher

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.ports import Clock

RECHECK_OVERLAP = timedelta(days=1)
"""Each poll looks one day back from the last one, in case a listing row appeared late."""


class LiveEvidence:
    """The Evidence port over live official sources, backed by a store."""

    def __init__(
        self,
        config: Config,
        fetcher: Fetcher,
        store: CorpusStore,
        *,
        clock: Clock | None = None,
        calendar: ReleaseCalendar | None = None,
        poll_interval_seconds: float = 300.0,
        initial_lookback_days: int = 30,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._store = store
        self._clock = clock or SystemClock()
        self._calendar = calendar
        self._poll_interval = timedelta(seconds=poll_interval_seconds)
        self._lookback = timedelta(days=initial_lookback_days)
        self._last_poll: datetime | None = None
        self.last_failures: list[str] = []
        """Pages that failed in the latest poll (refused, missing)."""

    def poll(self, *, force: bool = False) -> list[Document]:
        """Look for new documents and add them to the store; return the ones added.

        Does nothing (returns ``[]``) when the last poll was less than the poll
        interval ago, unless ``force``.
        """
        now = to_utc(self._clock.now())
        if (
            not force
            and self._last_poll is not None
            and now - self._last_poll < self._poll_interval
        ):
            return []
        start = (self._last_poll - RECHECK_OVERLAP) if self._last_poll else now - self._lookback
        known = frozenset(d.doc_id for d in self._store.documents())
        found: list[Document] = []
        failures: list[str] = []
        sources = self._config.sources
        if sources.federal_reserve:
            report = FedSource(self._fetcher).fetch_documents(start, now, refresh=True, skip=known)
            found += report.documents
            failures += report.failures
        if sources.bls:
            bls = BlsSource(self._fetcher, sources.manual_dir).fetch_documents(
                start, now, skip=known
            )
            found += bls.documents
            failures += bls.failures
        self._last_poll = now
        self.last_failures = failures
        return self._store.add(found, now=now)

    # -- Evidence port -------------------------------------------------------------------

    def documents(
        self, *, as_of: datetime, published_after: datetime | None = None
    ) -> list[Document]:
        """Documents with ``published_after < published_at <= as_of``, oldest first, after a poll."""
        self.poll()
        after = to_utc(published_after) if published_after is not None else None
        return self._store.between(after, to_utc(as_of))

    def get_document(self, doc_id: str, *, as_of: datetime) -> Document:
        """Raises ``NotFound`` if the document does not exist or was published after ``as_of``."""
        doc = self._store.get(doc_id)
        if doc is None:
            self.poll()
            doc = self._store.get(doc_id)
        if doc is None or doc.published_at > to_utc(as_of):
            raise NotFound(f"no document {doc_id!r} as of {to_utc(as_of).isoformat()}")
        return doc

    def scheduled_events(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        """Calendar entries with ``start < scheduled_at <= end``, earliest first."""
        if self._calendar is None:
            self._calendar, _ = build_calendar(
                self._fetcher,
                use_fomc=self._config.sources.federal_reserve,
                use_bls_site=self._config.sources.bls,
            )
        return self._calendar.between(start, end)
