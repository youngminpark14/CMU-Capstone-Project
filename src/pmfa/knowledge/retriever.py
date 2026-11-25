"""``ChromaKnowledge``: the ``Knowledge`` port over three Chroma indexes, plus ingestion.

Retrieval (the port): ``search_events``, ``search_evidence``, ``search_ledger``,
``get_passages`` and ``origins``. Each search takes ``as_of`` and the filter is
applied inside the Chroma query, before the nearest-neighbour search, so a
caller cannot read the future even by asking for it word for word.

Ingestion: ``index_documents``, ``index_events``, ``index_ledger`` and
``index_evidence`` (fill the evidence index from an ``Evidence`` port up to a
time). Re-indexing the same records leaves the index unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from pmfa.knowledge.chunking import chunk_document, passage_id
from pmfa.knowledge.embedders import Embedder, make_embedder
from pmfa.knowledge.index import (
    DOCUMENTS,
    EVENTS,
    EVIDENCE,
    LEDGER,
    ChromaIndex,
    Row,
    ceil_ts,
    floor_ts,
)
from pmfa.knowledge.origins import (
    DocumentSignature,
    Shingles,
    half_life_days,
    primary_sources,
    resolve_origins,
    shingles,
    text_hash,
)
from pmfa.knowledge.ranking import Candidate, rank
from pmfa.records.episode import LedgerEntry
from pmfa.records.evidence import Document, EventHit, LedgerHit, Origin, Passage, RetrievedPassage
from pmfa.records.market import Event

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.ports import Evidence


def event_text(event: Event) -> str:
    """The events-index document: title, shared rules text and strike list.

    The rules text is the first market's primary and secondary rules: markets of
    one event share their wording and differ only in the strike, which the
    strike list carries.
    """
    lines = [event.title]
    if event.sub_title:
        lines.append(event.sub_title)
    first = event.markets[0]
    rules = " ".join(r for r in (first.rules_primary, first.rules_secondary) if r)
    if rules:
        lines.append(f"Rules: {rules}")
    strikes = [m.subtitle or m.title for m in event.markets]
    lines.append("Markets: " + "; ".join(s for s in strikes if s))
    return "\n".join(lines)


def ledger_text(entry: LedgerEntry) -> str:
    """The ledger-index document: the question, which is what a new forecast is matched on."""
    return entry.question


def _passage_metadata(p: Passage) -> dict[str, Any]:
    return {
        "doc_id": p.doc_id,
        "origin_id": p.origin_id,
        "source": p.source,
        "url": p.url,
        "title": p.title,
        "published_at": p.published_at.isoformat(),
        "published_ts": ceil_ts(p.published_at),
        "index": p.index,
    }


def _passage(row_id: str, text: str, meta: dict[str, Any]) -> Passage:
    return Passage(
        passage_id=row_id,
        doc_id=str(meta["doc_id"]),
        origin_id=str(meta["origin_id"]),
        source=str(meta["source"]),
        url=str(meta["url"]),
        title=str(meta["title"]),
        published_at=datetime.fromisoformat(str(meta["published_at"])),
        index=int(meta["index"]),
        text=text,
    )


class ChromaKnowledge:
    """The three retrieval indexes, persistent in ``path`` or in memory.

    ``embedder`` defaults to the one ``config.retrieval.embedder`` names. An
    existing persistent index must be opened with the embedder that built it.
    """

    def __init__(
        self,
        config: Config,
        *,
        path: str | Path | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.config = config
        self.embedder = embedder or make_embedder(config.retrieval.embedder)
        self.index = ChromaIndex(self.embedder, path)
        self._shingles: dict[tuple[str, str], Shingles] = {}
        """Body shingles by ``(doc_id, text_hash)``: computed once per document per process."""

    def close(self) -> None:
        self.index.close()

    def __enter__(self) -> ChromaKnowledge:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- ingestion

    def index_documents(self, documents: list[Document]) -> int:
        """Chunk, embed and store documents; resolve origins over everything stored.

        Returns the number of passages written. Origins are recomputed over all
        stored documents, so a copy indexed before its original still ends up
        under the original's origin.
        """
        if not documents:
            return 0
        retrieval = self.config.retrieval
        # A document with no words has nothing to retrieve and no direction to compare.
        latest = {d.doc_id: d for d in documents if d.title.split() or d.text.split()}
        if not latest:
            return 0
        new_docs = sorted(latest.values(), key=lambda d: (d.published_at, d.doc_id))
        previous_counts = {
            row.id: int(row.metadata.get("passages", 0))
            for row in self.index.documents_by_id([d.doc_id for d in new_docs]).values()
        }

        # Chunk with a provisional origin and embed: the document vector is the mean
        # of its passage vectors, which every embedder can produce whatever its length limit.
        chunks = {
            d.doc_id: chunk_document(
                d,
                origin_id=d.doc_id,
                passage_words=retrieval.passage_words,
                overlap_words=retrieval.passage_overlap_words,
            )
            for d in new_docs
        }
        texts = [p.text for d in new_docs for p in chunks[d.doc_id]]
        vectors = self.index.embed(texts)
        passage_vectors: dict[str, np.ndarray] = {}
        doc_vectors: list[np.ndarray] = []
        offset = 0
        for d in new_docs:
            n = len(chunks[d.doc_id])
            block = vectors[offset : offset + n]
            passage_vectors[d.doc_id] = block
            mean = block.mean(axis=0)
            norm = float(np.linalg.norm(mean))
            doc_vectors.append(mean / norm if norm > 0 else mean)
            offset += n

        self.index.upsert(
            DOCUMENTS,
            ids=[d.doc_id for d in new_docs],
            vectors=np.stack(doc_vectors),
            metadatas=[
                {
                    "source": d.source,
                    "url": d.url,
                    "title": d.title,
                    "kind": d.kind,
                    "published_at": d.published_at.isoformat(),
                    "published_ts": ceil_ts(d.published_at),
                    "text_hash": text_hash(f"{d.title}\n{d.text}"),
                    "passages": len(chunks[d.doc_id]),
                    "origin_id": d.doc_id,
                }
                for d in new_docs
            ],
            # The body is kept so origin resolution can compare texts when a
            # reopened index takes new documents.
            documents=[d.text for d in new_docs],
        )

        stored = self.index.all_documents()
        origin = resolve_origins(
            (self._signature(row) for row in stored), threshold=retrieval.origin_copy_overlap
        )

        written = 0
        for d in new_docs:
            passages = [
                p.model_copy(update={"origin_id": origin[d.doc_id]}) for p in chunks[d.doc_id]
            ]
            self.index.upsert(
                EVIDENCE,
                ids=[p.passage_id for p in passages],
                vectors=passage_vectors[d.doc_id],
                metadatas=[_passage_metadata(p) for p in passages],
                documents=[p.text for p in passages],
            )
            stale = range(len(passages), previous_counts.get(d.doc_id, 0))
            self.index.delete(EVIDENCE, [passage_id(d.doc_id, n) for n in stale])
            written += len(passages)

        # Record resolved origins; re-tag the passages of older documents whose
        # origin moved because an earlier original arrived late.
        new_ids = {d.doc_id for d in new_docs}
        for row in stored:
            resolved = origin[row.id]
            if row.metadata.get("origin_id") == resolved:
                continue
            self.index.update_metadata(DOCUMENTS, [row.id], [{"origin_id": resolved}])
            if row.id not in new_ids:
                count = int(row.metadata.get("passages", 0))
                ids = [passage_id(row.id, n) for n in range(count)]
                self.index.update_metadata(EVIDENCE, ids, [{"origin_id": resolved}] * count)
        return written

    def _signature(self, row: Row) -> DocumentSignature:
        """What origin resolution compares: the stored body's shingles and its text hash.

        A row written before bodies were stored has no body; it still matches
        identical texts by hash, and nothing by overlap.
        """
        digest = str(row.metadata["text_hash"])
        key = (row.id, digest)
        if key not in self._shingles:
            self._shingles[key] = shingles(row.document or "")
        return DocumentSignature(
            doc_id=row.id,
            published_at=datetime.fromisoformat(str(row.metadata["published_at"])),
            text_hash=digest,
            shingles=self._shingles[key],
        )

    def index_evidence(
        self, evidence: Evidence, *, as_of: datetime, published_after: datetime | None = None
    ) -> int:
        """Fill the evidence index from an ``Evidence`` port with what it published up to ``as_of``."""
        return self.index_documents(
            evidence.documents(as_of=as_of, published_after=published_after)
        )

    def index_events(self, events: list[Event]) -> int:
        """Store one document per event. An event needs markets: they carry its open and close."""
        if not events:
            return 0
        for event in events:
            if not event.markets:
                raise ValueError(f"event {event.event_ticker} has no markets to index")
        latest = list({e.event_ticker: e for e in events}.values())
        texts = [event_text(e) for e in latest]
        self.index.upsert(
            EVENTS,
            ids=[e.event_ticker for e in latest],
            vectors=self.index.embed(texts),
            metadatas=[
                {
                    "series_ticker": e.series_ticker,
                    "category": e.category,
                    "open_ts": ceil_ts(e.open_time),
                    "close_ts": floor_ts(e.close_time),
                }
                for e in latest
            ],
            documents=texts,
        )
        return len(latest)

    def index_ledger(self, entries: list[LedgerEntry]) -> int:
        """Store one document per resolved forecast.

        Entries without an outcome, a resolution time, or a market price are
        skipped: they cannot serve as calibration examples yet.
        """
        rows = [
            (e, metadata)
            for e in {e.entry_id: e for e in entries}.values()
            if (metadata := _ledger_metadata(e)) is not None
        ]
        if not rows:
            return 0
        texts = [ledger_text(e) for e, _ in rows]
        self.index.upsert(
            LEDGER,
            ids=[e.entry_id for e, _ in rows],
            vectors=self.index.embed(texts),
            metadatas=[metadata for _, metadata in rows],
            documents=texts,
        )
        return len(rows)

    # -- the Knowledge port

    def search_events(self, query: str, *, as_of: datetime, k: int = 5) -> list[EventHit]:
        hits = self.index.query_events(self._query_vector(query), as_of=as_of, n=k)
        return [EventHit(event_ticker=h.id, similarity=h.similarity) for h in hits]

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        retrieval = self.config.retrieval
        queries = [q for q in queries if q.strip()]
        if not queries or k <= 0:
            return []
        per_query = max(4 * k, 32)
        vectors = self.index.embed(queries)
        candidates: list[Candidate] = []
        for query, vector in zip(queries, vectors, strict=True):
            for hit in self.index.query_evidence(
                vector, as_of=as_of, n=per_query, published_after=published_after
            ):
                candidates.append(
                    Candidate(
                        passage=_passage(hit.id, hit.document, hit.metadata),
                        similarity=hit.similarity,
                        query=query,
                        vector=hit.vector,
                    )
                )
        return rank(
            candidates,
            as_of=as_of,
            half_life_days=half_life_days(self.config, category),
            k=k,
            max_per_origin=retrieval.max_per_origin,
            similarity_floor=retrieval.similarity_floor,
            near_duplicate_similarity=retrieval.near_duplicate_similarity,
            mode=retrieval.ranking,
        )

    def search_ledger(
        self, query: str, *, as_of: datetime, category: str, k: int = 5
    ) -> list[LedgerHit]:
        vector = self._query_vector(query)
        same = self.index.query_ledger(vector, as_of=as_of, n=k, category=category)
        other = self.index.query_ledger(
            vector, as_of=as_of, n=k - len(same), exclude_category=category
        )
        return [
            LedgerHit(
                question=h.document,
                category=str(h.metadata["category"]),
                probability=float(h.metadata["probability"]),
                market_price=float(h.metadata["market_price"]),
                outcome=bool(h.metadata["outcome"]),
                resolved_at=datetime.fromisoformat(str(h.metadata["resolved_at"])),
                similarity=h.similarity,
            )
            for h in [*same, *other]
        ]

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        rows = {r.id: r for r in self.index.get_evidence(passage_ids, as_of=as_of)}
        ordered = dict.fromkeys(pid for pid in passage_ids if pid in rows)
        return [_passage(pid, rows[pid].document, rows[pid].metadata) for pid in ordered]

    def origins(self, passages: list[Passage], *, category: str) -> list[Origin]:
        """One ``Origin`` per distinct origin id, in first-seen order.

        The origin's source, URL and publish time come from its document. A
        passage whose origin document is not stored describes its own origin.
        """
        primaries = primary_sources(self.config, category)
        first_seen: dict[str, Passage] = {}
        for p in passages:
            first_seen.setdefault(p.origin_id, p)
        stored = self.index.documents_by_id(list(first_seen))
        result = []
        for origin_id, p in first_seen.items():
            row = stored.get(origin_id)
            if row is not None:
                source = str(row.metadata["source"])
                url = str(row.metadata["url"])
                published_at = datetime.fromisoformat(str(row.metadata["published_at"]))
            else:
                source, url, published_at = p.source, p.url, p.published_at
            result.append(
                Origin(
                    origin_id=origin_id,
                    source=source,
                    url=url,
                    published_at=published_at,
                    is_primary=source in primaries,
                )
            )
        return result

    def _query_vector(self, query: str) -> np.ndarray:
        return self.index.embed([query])[0]


def _ledger_metadata(e: LedgerEntry) -> dict[str, Any] | None:
    """Index metadata for a resolved entry, or ``None`` when it is not resolved and priced."""
    if e.outcome is None or e.resolved_at is None or e.market_price is None:
        return None
    return {
        "category": e.category,
        "system": e.system,
        "probability": float(e.probability),
        "market_price": float(e.market_price),
        "outcome": bool(e.outcome),
        "resolved_at": e.resolved_at.isoformat(),
        "resolved_ts": ceil_ts(e.resolved_at),
        "event_ticker": e.event_ticker,
        "market_ticker": e.market_ticker,
    }


__all__ = ["ChromaKnowledge", "event_text", "ledger_text"]
