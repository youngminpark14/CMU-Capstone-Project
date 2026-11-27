"""The evidence corpus: a store on disk, the frozen Evidence adapter, and the one-command build.

The corpus is rebuilt by one command (``build_corpus``) and never committed:
``data/`` is ignored by git, and the pages in it are public-domain text that
anyone can fetch again.

On disk
-------
A :class:`CorpusStore` is a directory with three files:

- ``documents.jsonl``: one :class:`~pmfa.records.evidence.Document` per line;
- ``events.jsonl``: the release calendar, one ``ScheduledEvent`` per line;
- ``manifest.json``: when the corpus was built, the window it covers, the
  document count per source, the content hash of every document, and the pages
  that could not be fetched.

Backtests read it through :class:`FrozenEvidence`, which applies the as-of rule
inside each call: nothing published after ``as_of`` is ever returned, whatever
the caller passes.
"""

from __future__ import annotations

import json
import os
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pmfa.errors import NotFound
from pmfa.records.base import Record, UtcTime
from pmfa.records.evidence import Document, ScheduledEvent
from pmfa.world.evidence.bls import BlsSource
from pmfa.world.evidence.calendar import ReleaseCalendar, build_calendar
from pmfa.world.evidence.common import content_hash, to_utc
from pmfa.world.evidence.fed import FedSource
from pmfa.world.evidence.http import Fetcher

if TYPE_CHECKING:
    from pmfa.config import Config


class CorpusManifest(Record):
    """What a corpus holds and how it was built."""

    built_at: UtcTime
    window_start: UtcTime | None = None
    window_end: UtcTime | None = None
    counts: dict[str, int] = {}
    """Documents per source, e.g. ``{"bls.gov": 24, "federalreserve.gov": 59}``."""
    content_hashes: dict[str, str] = {}
    """``doc_id`` to the SHA-256 of its text."""
    failures: list[str] = []
    """Pages that could not be fetched, one line each. Empty on a clean build."""
    notes: list[str] = []
    """Where the calendar came from."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class CorpusStore:
    """Documents as one JSON-lines file, the calendar as another, plus a manifest."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._docs: dict[str, Document] | None = None
        self._sorted: list[Document] = []
        self._times: list[datetime] = []

    @property
    def documents_path(self) -> Path:
        return self.directory / "documents.jsonl"

    @property
    def events_path(self) -> Path:
        return self.directory / "events.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    # -- reading -------------------------------------------------------------------------

    def _load(self) -> dict[str, Document]:
        if self._docs is None:
            docs: dict[str, Document] = {}
            if self.documents_path.exists():
                for line in self.documents_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        doc = Document.model_validate_json(line)
                        docs[doc.doc_id] = doc
            self._set(docs)
        assert self._docs is not None
        return self._docs

    def _set(self, docs: dict[str, Document]) -> None:
        self._docs = docs
        self._sorted = sorted(docs.values(), key=lambda d: (d.published_at, d.doc_id))
        self._times = [d.published_at for d in self._sorted]

    def documents(self) -> list[Document]:
        """Every document, oldest first."""
        self._load()
        return list(self._sorted)

    def get(self, doc_id: str) -> Document | None:
        return self._load().get(doc_id)

    def between(self, after: datetime | None, until: datetime) -> list[Document]:
        """Documents with ``after < published_at <= until``, oldest first (``after=None``: no lower bound)."""
        self._load()
        lo = 0 if after is None else bisect_right(self._times, after)
        hi = bisect_right(self._times, until)
        return self._sorted[lo:hi] if lo < hi else []

    def events(self) -> list[ScheduledEvent]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return [ScheduledEvent.model_validate_json(ln) for ln in lines if ln.strip()]

    def manifest(self) -> CorpusManifest | None:
        if not self.manifest_path.exists():
            return None
        return CorpusManifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))

    def verify(self) -> list[str]:
        """Doc ids whose text no longer matches the hash in the manifest or in the document."""
        manifest = self.manifest()
        recorded = manifest.content_hashes if manifest else {}
        bad = []
        for doc in self.documents():
            changed = doc.content_hash != content_hash(doc.text)
            if changed or recorded.get(doc.doc_id, doc.content_hash) != doc.content_hash:
                bad.append(doc.doc_id)
        return bad

    # -- writing -------------------------------------------------------------------------

    def write(
        self,
        documents: Iterable[Document],
        *,
        events: Iterable[ScheduledEvent] = (),
        manifest: CorpusManifest,
    ) -> CorpusManifest:
        """Replace the corpus. The manifest's counts and hashes are recomputed from ``documents``."""
        docs = {d.doc_id: d for d in documents}
        ordered = sorted(docs.values(), key=lambda d: (d.published_at, d.doc_id))
        _atomic_write(self.documents_path, "".join(d.model_dump_json() + "\n" for d in ordered))
        _atomic_write(
            self.events_path,
            "".join(
                e.model_dump_json() + "\n"
                for e in sorted(events, key=lambda e: (e.scheduled_at, e.scheduled_event_id))
            ),
        )
        self._set(docs)
        return self._write_manifest(manifest)

    def add(self, documents: Iterable[Document], *, now: datetime | None = None) -> list[Document]:
        """Append documents whose ``doc_id`` is new; return those that were added.

        A document already stored is left as it is: the live poller adds what
        is new, it does not rewrite history. ``now`` extends the manifest's window end.
        """
        existing = self._load()
        added = [d for d in documents if d.doc_id not in existing]
        added = list({d.doc_id: d for d in added}.values())
        if added:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.documents_path.open("a", encoding="utf-8") as fh:
                fh.writelines(d.model_dump_json() + "\n" for d in added)
            self._set({**existing, **{d.doc_id: d for d in added}})
        old = self.manifest()
        stamp = to_utc(now) if now is not None else datetime.now(UTC)
        base = old or CorpusManifest(built_at=stamp)
        end = base.window_end
        if now is not None and (end is None or to_utc(now) > end):
            end = to_utc(now)
        self._write_manifest(base.model_copy(update={"window_end": end}))
        return added

    def _write_manifest(self, manifest: CorpusManifest) -> CorpusManifest:
        docs = self.documents()
        final = manifest.model_copy(
            update={
                "counts": dict(sorted(Counter(d.source for d in docs).items())),
                "content_hashes": {d.doc_id: d.content_hash for d in docs},
            }
        )
        _atomic_write(self.manifest_path, final.model_dump_json(indent=2) + "\n")
        return final


class FrozenEvidence:
    """The Evidence port over a stored corpus. The as-of rule is applied inside every method."""

    def __init__(self, store: CorpusStore, calendar: ReleaseCalendar | None = None) -> None:
        self._store = store
        self._calendar = calendar if calendar is not None else ReleaseCalendar(store.events())

    def documents(
        self, *, as_of: datetime, published_after: datetime | None = None
    ) -> list[Document]:
        """Documents with ``published_after < published_at <= as_of``, oldest first."""
        after = to_utc(published_after) if published_after is not None else None
        return self._store.between(after, to_utc(as_of))

    def get_document(self, doc_id: str, *, as_of: datetime) -> Document:
        """Raises ``NotFound`` if the document does not exist or was published after ``as_of``."""
        doc = self._store.get(doc_id)
        if doc is None or doc.published_at > to_utc(as_of):
            raise NotFound(f"no document {doc_id!r} as of {to_utc(as_of).isoformat()}")
        return doc

    def scheduled_events(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        """Calendar entries with ``start < scheduled_at <= end``, earliest first."""
        return self._calendar.between(start, end)


def build_corpus(
    config: Config,
    start: datetime,
    end: datetime,
    fetcher: Fetcher,
    *,
    store: CorpusStore | None = None,
    now: datetime | None = None,
) -> CorpusManifest:
    """Fetch every followed source for ``start <= published_at <= end`` and write the corpus.

    Federal Reserve documents come from the listings; BLS releases from manual
    files in ``config.sources.manual_dir`` and, while BLS lets the client in,
    from the site. A page that fails is listed in the manifest's ``failures``
    and the build goes on. The store defaults to ``config.paths.corpus_dir()``.
    ``now`` is the build time written to the manifest (the system clock if omitted).
    """
    start, end = to_utc(start), to_utc(end)
    store = store or CorpusStore(config.paths.corpus_dir())
    sources = config.sources
    calendar, notes = build_calendar(
        fetcher, use_fomc=sources.federal_reserve, use_bls_site=sources.bls
    )
    documents: list[Document] = []
    failures: list[str] = []
    if sources.federal_reserve:
        report = FedSource(fetcher).fetch_documents(start, end)
        documents += report.documents
        failures += report.failures
    if sources.bls:
        bls = BlsSource(fetcher, sources.manual_dir).fetch_documents(start, end)
        documents += bls.documents
        failures += bls.failures
    manifest = CorpusManifest(
        built_at=to_utc(now) if now is not None else datetime.now(UTC),
        window_start=start,
        window_end=end,
        failures=failures,
        notes=notes,
    )
    return store.write(documents, events=calendar.events, manifest=manifest)


def manifest_json(manifest: CorpusManifest) -> str:
    """The manifest as indented JSON, for the CLI to print."""
    return json.dumps(manifest.model_dump(mode="json"), indent=2)
