"""Live evidence for the shadow and demo modes.

:class:`LiveEvidence` implements the Evidence port by polling the listing
sources (the Federal Reserve's press-release and speech JSON, BLS releases, and
any RSS or Atom feeds in ``config.sources.feeds``, see
``pmfa.world.evidence.feed``) and adding what is new to a
:class:`~pmfa.world.evidence.corpus.CorpusStore`. Reads then come from the store
with the same as-of rule as the frozen corpus, so shadow mode and a backtest
filter documents the same way. A feed whose host is a primary source of a
followed series is refused: feeds are secondary evidence only.

Politeness: a poll costs two listing requests to federalreserve.gov, one request
per listed feed, plus one page per new document, spaced by the fetcher, and
calls to ``documents`` poll at most once per ``poll_interval_seconds`` of the
clock. Pages already in the store are not fetched again by a poll. If BLS
refuses automated access, that is remembered by the fetcher's cache and only
manual files (see ``pmfa.world.evidence.bls``) bring in BLS text.

Re-checking a source (``recheck``): the stored copy of a document never changes
(the as-of rule and replay depend on it), so the self-halt check for a changed
or withdrawn source behind an open position asks ``recheck`` instead of
``get_document``. It fetches the page again from the site, bypassing the cache,
and rebuilds the document the way a poll would, without writing it to the
store. A 404 or 410, or a page that no longer parses, is a withdrawal
(``NotFound``). When the source cannot be asked (a refusal, a server error, a
dropped connection, a BLS release that came from a manual file) the stored copy
is returned, so a transient failure never halts on its own; the reason is kept
in ``last_failures``. Each document is re-checked at most once per poll interval.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pmfa.errors import NotFound
from pmfa.records.evidence import Document, ScheduledEvent
from pmfa.world.clocks import SystemClock
from pmfa.world.evidence.bls import BlsSource, ManualImportError
from pmfa.world.evidence.calendar import ReleaseCalendar, build_calendar
from pmfa.world.evidence.common import BLS_SOURCE, FED_SOURCE, to_utc
from pmfa.world.evidence.corpus import CorpusStore
from pmfa.world.evidence.fed import FedSource
from pmfa.world.evidence.feed import FeedError, FeedSource, feed_host
from pmfa.world.evidence.http import Fetcher, FetchError, FetchRefused

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.ports import Clock

RECHECK_OVERLAP = timedelta(days=1)
"""Each poll looks one day back from the last one, in case a listing row appeared late."""

WITHDRAWN_STATUSES = frozenset({404, 410})
"""Answers that mean the page is gone, not that the site is unavailable."""


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
        self._rechecked: dict[str, tuple[datetime, Document | NotFound]] = {}
        self.last_failures: list[str] = []
        """Pages that failed in the latest poll (refused, missing) or re-check since it."""

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
        primary = self._primary_sources()
        for url in sources.feeds:
            if feed_host(url) in primary:
                failures.append(
                    f"feed {url}: {feed_host(url)} is a primary source; feeds are secondary only"
                )
                continue
            feed = FeedSource(self._fetcher).fetch_documents(
                url, start, now, refresh=True, skip=known
            )
            found += feed.documents
            failures += feed.failures
        self._last_poll = now
        self.last_failures = failures
        return self._store.add(found, now=now)

    def _primary_sources(self) -> frozenset[str]:
        return frozenset(src for s in self._config.follow.series for src in s.primary_sources)

    # -- re-checking a source ------------------------------------------------------------

    def recheck(self, doc_id: str, *, as_of: datetime) -> Document:
        """The document as its source serves it now; ``NotFound`` if it is gone.

        Raises ``NotFound`` too when the document was never stored or was
        published after ``as_of``. See the module docstring for what counts as
        gone and what returns the stored copy. Nothing is written to the store.
        """
        stored = self.get_document(doc_id, as_of=as_of)
        now = to_utc(self._clock.now())
        seen = self._rechecked.get(doc_id)
        if seen is not None and now - seen[0] < self._poll_interval:
            result = seen[1]
        else:
            result = self._refetch(stored)
            self._rechecked[doc_id] = (now, result)
        if isinstance(result, NotFound):
            raise NotFound(str(result))
        return result

    def _refetch(self, stored: Document) -> Document | NotFound:
        try:
            if stored.source == FED_SOURCE:
                return FedSource(self._fetcher).refetch(stored)
            if stored.source == BLS_SOURCE:
                return self._refetch_bls(stored)
            feed = next(
                (u for u in self._config.sources.feeds if feed_host(u) == stored.source), None
            )
            if feed is not None:
                return FeedSource(self._fetcher).refetch(feed, stored) or stored
        except FetchRefused as exc:
            self.last_failures.append(f"re-check of {stored.doc_id} refused: {exc}")
            return stored
        except FetchError as exc:
            if exc.status in WITHDRAWN_STATUSES:
                return NotFound(f"{stored.doc_id} is gone from {stored.url}: {exc}")
            self.last_failures.append(f"re-check of {stored.doc_id} failed: {exc}")
            return stored
        except (FeedError, ManualImportError) as exc:
            self.last_failures.append(f"re-check of {stored.doc_id} failed: {exc}")
            return stored
        except ValueError as exc:
            return NotFound(f"{stored.doc_id} at {stored.url} no longer parses as a page: {exc}")
        return stored  # a source this class does not know how to ask again

    def _refetch_bls(self, stored: Document) -> Document:
        """A BLS release from the manual file a person saved, else the stored copy.

        BLS refuses this client (see ``pmfa.world.evidence.bls``), so the manual
        file is the only copy that can be read again here.
        """
        manual = BlsSource(None, self._config.sources.manual_dir).import_manual()
        return next((d for d in manual if d.doc_id == stored.doc_id), stored)

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
