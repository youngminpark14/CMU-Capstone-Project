"""The Chroma collections behind retrieval, with the as-of rule built into every query.

Three retrieval collections:

- ``events``: one document per event; returned only while ``open_time <= as_of < close_time``.
- ``evidence``: one row per passage; returned only when ``published_at <= as_of``.
- ``ledger``: one row per resolved forecast; returned only when ``resolved_at < as_of``.

A fourth, internal collection, ``documents``, holds one row per evidence
document: its body text, metadata and mean passage vector. Origin resolution
compares the bodies, and the collection lets a reopened persistent index keep
resolving origins for new documents. It is never searched by a retrieval call.

Times are stored as integer epoch seconds. Rounding is always toward exclusion:
a publish time is rounded up and an as-of time down, so a document published a
fraction of a second after the as-of time can never pass the filter. Embeddings
are always passed explicitly, so Chroma never loads its own model, and
telemetry is off.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from pmfa.knowledge.embedders import Embedder, EmbedderMismatch

EVENTS = "events"
EVIDENCE = "evidence"
LEDGER = "ledger"
DOCUMENTS = "documents"
COLLECTIONS = (EVENTS, EVIDENCE, LEDGER, DOCUMENTS)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EMBEDDER_KEY = "pmfa_embedder"


def _require_aware(at: datetime) -> None:
    if at.tzinfo is None:
        raise ValueError("naive datetime: as-of times are timezone-aware UTC")


def floor_ts(at: datetime) -> int:
    """Whole epoch seconds, rounded down."""
    _require_aware(at)
    delta = at - _EPOCH
    return delta.days * 86_400 + delta.seconds


def ceil_ts(at: datetime) -> int:
    """Whole epoch seconds, rounded up."""
    _require_aware(at)
    delta = at - _EPOCH
    return delta.days * 86_400 + delta.seconds + (1 if delta.microseconds else 0)


@dataclass(frozen=True)
class Hit:
    """One row from a collection: id, cosine similarity, stored text, metadata, vector."""

    id: str
    similarity: float
    document: str
    metadata: dict[str, Any]
    vector: np.ndarray | None = None


@dataclass(frozen=True)
class Row:
    """One stored row read by id."""

    id: str
    document: str
    metadata: dict[str, Any]
    vector: np.ndarray | None = None


def _settings() -> Any:
    from chromadb.config import Settings

    return Settings(anonymized_telemetry=False, allow_reset=False)


class ChromaIndex:
    """The collections of one knowledge store, persistent in ``path`` or in memory.

    Every in-memory instance gets its own Chroma database, because chromadb
    shares a single in-memory system across all ephemeral clients of a process.
    """

    def __init__(self, embedder: Embedder, path: str | Path | None = None) -> None:
        import chromadb

        self.embedder = embedder
        self.path = Path(path) if path is not None else None
        self._database: str | None = None
        if self.path is not None:
            self.path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.path), settings=_settings())
        else:
            self._database = f"pmfa_{uuid.uuid4().hex}"
            chromadb.AdminClient(_settings()).create_database(self._database)
            self._client = chromadb.EphemeralClient(settings=_settings(), database=self._database)
        self._collections = {name: self._open(name) for name in COLLECTIONS}

    def _open(self, name: str) -> Any:
        collection = self._client.get_or_create_collection(
            name,
            configuration={"hnsw": {"space": "cosine"}},
            metadata={_EMBEDDER_KEY: self.embedder.identity},
            embedding_function=None,
        )
        built_by = (collection.metadata or {}).get(_EMBEDDER_KEY)
        if built_by != self.embedder.identity:
            raise EmbedderMismatch(
                f"the {name} index was built by {built_by!r} and cannot be queried with "
                f"{self.embedder.identity!r}; rebuild it or open it with the same embedder"
            )
        return collection

    def close(self) -> None:
        """Release the client. An in-memory index is dropped with it."""
        import chromadb

        if self._database is not None:
            chromadb.AdminClient(_settings()).delete_database(self._database)
            self._database = None
        else:
            self._client.close()

    # -- vectors

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed with the index's own embedder: the only embedder its vectors match."""
        vectors = self.embedder.embed(texts)
        if vectors.shape != (len(texts), self.embedder.dimension):
            raise EmbedderMismatch("the embedder returned vectors of the wrong shape")
        return vectors

    # -- writes (ingestion; nothing here reads for a caller)

    def upsert(
        self,
        name: str,
        *,
        ids: list[str],
        vectors: np.ndarray,
        metadatas: list[dict[str, Any]],
        documents: list[str] | None = None,
    ) -> None:
        """Insert or replace rows. The same ids and content always leave the same state."""
        if not ids:
            return
        collection = self._collections[name]
        batch = self._client.get_max_batch_size()
        for start in range(0, len(ids), batch):
            stop = start + batch
            collection.upsert(
                ids=ids[start:stop],
                embeddings=vectors[start:stop],
                metadatas=metadatas[start:stop],
                documents=documents[start:stop] if documents is not None else None,
            )

    def update_metadata(self, name: str, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        """Merge metadata fields into existing rows."""
        if ids:
            self._collections[name].update(ids=ids, metadatas=metadatas)

    def delete(self, name: str, ids: list[str]) -> None:
        if ids:
            self._collections[name].delete(ids=ids)

    def all_documents(self) -> list[Row]:
        """Every stored document row with its body, for origin resolution at ingestion time."""
        result = self._collections[DOCUMENTS].get(include=["metadatas", "documents"])
        return _rows(result)

    def documents_by_id(self, doc_ids: list[str]) -> dict[str, Row]:
        """Stored document rows by id, for describing origins."""
        if not doc_ids:
            return {}
        result = self._collections[DOCUMENTS].get(ids=list(dict.fromkeys(doc_ids)))
        return {row.id: row for row in _rows(result)}

    def count(self, name: str) -> int:
        return int(self._collections[name].count())

    # -- reads: each one carries its as-of filter, and there is no other way in

    def query_evidence(
        self,
        vector: np.ndarray,
        *,
        as_of: datetime,
        n: int,
        published_after: datetime | None = None,
    ) -> list[Hit]:
        """Passages with ``published_after < published_at <= as_of``, nearest first."""
        clauses: list[dict[str, Any]] = [{"published_ts": {"$lte": floor_ts(as_of)}}]
        if published_after is not None:
            clauses.append({"published_ts": {"$gt": ceil_ts(published_after)}})
        return self._query(EVIDENCE, vector, n, _all_of(clauses), with_vectors=True)

    def get_evidence(self, passage_ids: list[str], *, as_of: datetime) -> list[Row]:
        """Passages by id, published at or before ``as_of``. Missing ids are left out."""
        if not passage_ids:
            return []
        result = self._collections[EVIDENCE].get(
            ids=list(dict.fromkeys(passage_ids)),
            where={"published_ts": {"$lte": floor_ts(as_of)}},
            include=["metadatas", "documents"],
        )
        return _rows(result)

    def query_events(self, vector: np.ndarray, *, as_of: datetime, n: int) -> list[Hit]:
        """Events open at ``as_of``, each series' next event to close before its later ones.

        The listings with ``open_ts <= as_of < close_ts`` are ordered by their
        place in their series (the event that closes first, then the one after
        it, and so on), then by similarity to ``vector``, then by row id. So
        the first hit of each series is its next event. An exchange lists
        several months or meetings of one series at once, and a document is
        evidence for the nearest of them first; ranked by similarity alone, the
        later months of one series crowd the nearest event of another series
        out of a short list. The events are few, so every current listing is
        read and ranked here; the time filter is inside the read.

        Both bounds compare with the as-of time rounded down. That is still safe
        at the close, because ``close_ts`` is stored rounded down too, and
        ``floor(close) > floor(as_of)`` implies ``close > as_of``. Using one
        rounding for both makes back-to-back listings of an event meet without
        a gap or an overlap.
        """
        if n <= 0:
            return []
        at = floor_ts(as_of)
        where = _all_of([{"open_ts": {"$lte": at}}, {"close_ts": {"$gt": at}}])
        rows = _rows(
            self._collections[EVENTS].get(
                where=where, include=["metadatas", "documents", "embeddings"]
            )
        )
        query = np.asarray(vector, dtype=np.float64)
        by_series: dict[str, list[tuple[int, float, Row]]] = {}
        for row in rows:
            series = str(row.metadata.get("series_ticker") or row.id)
            # A row written before the event's own close was stored falls back on its listing's.
            closes = int(row.metadata.get("event_close_ts", row.metadata["close_ts"]))
            by_series.setdefault(series, []).append((closes, _cosine(query, row.vector), row))
        ordered: list[tuple[int, float, str, float, Row]] = []
        for listed in by_series.values():
            listed.sort(key=lambda item: (item[0], -round(item[1], _TIE_DECIMALS), item[2].id))
            ordered.extend(
                (place, -round(similarity, _TIE_DECIMALS), row.id, similarity, row)
                for place, (_, similarity, row) in enumerate(listed)
            )
        ordered.sort(key=lambda item: item[:3])
        return [
            Hit(id=row.id, similarity=similarity, document=row.document, metadata=row.metadata)
            for _, _, _, similarity, row in ordered[:n]
        ]

    def query_ledger(
        self,
        vector: np.ndarray,
        *,
        as_of: datetime,
        n: int,
        category: str | None = None,
        exclude_category: str | None = None,
    ) -> list[Hit]:
        """The ``n`` ledger entries most like ``vector`` with ``resolved_at < as_of``.

        Optionally in or outside one category. The search is exact and its order
        is fixed: by similarity, then the later resolution, then the id. One
        market forecast at several horizons gives several rows with the same
        text, so ties are the rule here, and the nearest-neighbour index returns
        tied rows in an order that changes from run to run. The ledger holds one
        row per resolved forecast, few enough to read every eligible one and
        rank them. The time filter is still inside the read.
        """
        if n <= 0:
            return []
        clauses: list[dict[str, Any]] = [{"resolved_ts": {"$lt": ceil_ts(as_of)}}]
        if category is not None:
            clauses.append({"category": {"$eq": category}})
        if exclude_category is not None:
            clauses.append({"category": {"$ne": exclude_category}})
        rows = _rows(
            self._collections[LEDGER].get(
                where=_all_of(clauses), include=["metadatas", "documents", "embeddings"]
            )
        )
        query = np.asarray(vector, dtype=np.float64)
        scored = [(_cosine(query, row.vector), row) for row in rows]
        scored.sort(
            key=lambda pair: (
                -round(pair[0], _TIE_DECIMALS),
                -int(pair[1].metadata["resolved_ts"]),
                pair[1].id,
            )
        )
        return [
            Hit(id=row.id, similarity=similarity, document=row.document, metadata=row.metadata)
            for similarity, row in scored[:n]
        ]

    def _query(
        self,
        name: str,
        vector: np.ndarray,
        n: int,
        where: dict[str, Any],
        *,
        with_vectors: bool = False,
    ) -> list[Hit]:
        if n <= 0:
            return []
        include = ["metadatas", "documents", "distances"]
        if with_vectors:
            include.append("embeddings")
        result = self._collections[name].query(
            query_embeddings=[np.asarray(vector, dtype=np.float32)],
            n_results=n,
            where=where,
            include=include,
        )
        ids = result["ids"][0]
        distances = result["distances"][0]
        documents = result["documents"][0]
        metadatas = result["metadatas"][0]
        vectors = result["embeddings"][0] if with_vectors else None
        return [
            Hit(
                id=ids[i],
                similarity=1.0 - float(distances[i]),
                document=documents[i] or "",
                metadata=dict(metadatas[i] or {}),
                vector=np.asarray(vectors[i], dtype=np.float32) if vectors is not None else None,
            )
            for i in range(len(ids))
        ]


_TIE_DECIMALS = 6
"""Similarities equal to this many decimals are a tie: stored vectors are 32-bit floats."""


def _cosine(query: np.ndarray, stored: np.ndarray | None) -> float:
    """Cosine similarity of the query and one stored vector, 0.0 when either has no length."""
    if stored is None:
        return 0.0
    other = np.asarray(stored, dtype=np.float64)
    norm = float(np.linalg.norm(query)) * float(np.linalg.norm(other))
    return float(np.dot(query, other)) / norm if norm > 0 else 0.0


def _all_of(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _rows(result: Any) -> list[Row]:
    ids = result["ids"]
    documents = result.get("documents")
    metadatas = result.get("metadatas")
    embeddings = result.get("embeddings")
    rows = []
    for i, row_id in enumerate(ids):
        rows.append(
            Row(
                id=row_id,
                document=(documents[i] or "") if documents is not None else "",
                metadata=dict(metadatas[i] or {}) if metadatas is not None else {},
                vector=(
                    np.asarray(embeddings[i], dtype=np.float32) if embeddings is not None else None
                ),
            )
        )
    return rows


__all__ = [
    "COLLECTIONS",
    "DOCUMENTS",
    "EVENTS",
    "EVIDENCE",
    "LEDGER",
    "ChromaIndex",
    "Hit",
    "Row",
    "ceil_ts",
    "floor_ts",
]
