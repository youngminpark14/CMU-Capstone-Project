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

Every search is exact and repeatable. Chroma stores the rows and applies the
time filter inside the read; the rows that pass it are ranked here, by cosine
similarity and then by a fixed tie-break. Chroma's own nearest-neighbour search
is approximate: on the frozen data it returned different passages for the same
query in different runs, and tied rows in a different order, so two identical
backtests asked the models different questions. The cost is a read of every
eligible row per search: about 0.2 s for the backtest's 700 passages, about
1 s and 850 MB for 5,000 rows and about 4 s and 2.8 GB for 20,000 (2048-number
vectors). A collection much larger than that needs another design.
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
        """Passages with ``published_after < published_at <= as_of``, most similar first.

        Passages equally similar (to six decimals) come in id order.
        """
        if n <= 0:
            return []
        clauses: list[dict[str, Any]] = [{"published_ts": {"$lte": floor_ts(as_of)}}]
        if published_after is not None:
            clauses.append({"published_ts": {"$gt": ceil_ts(published_after)}})
        scored = self._scored(EVIDENCE, vector, _all_of(clauses))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [
            Hit(
                id=row.id,
                similarity=similarity,
                document=row.document,
                metadata=row.metadata,
                vector=row.vector,
            )
            for similarity, row in scored[:n]
        ]

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
        by_series: dict[str, list[tuple[int, float, Row]]] = {}
        for similarity, row in self._scored(EVENTS, vector, where):
            series = str(row.metadata.get("series_ticker") or row.id)
            # A row written before the event's own close was stored cannot be placed (its
            # listing's close is not the event's), so it comes after the rows that can.
            closes = int(row.metadata.get("event_close_ts", _CLOSE_UNKNOWN))
            by_series.setdefault(series, []).append((closes, similarity, row))
        ordered: list[tuple[int, float, str, float, Row]] = []
        for listed in by_series.values():
            listed.sort(key=lambda item: (item[0], -item[1], item[2].id))
            ordered.extend(
                (place, -similarity, row.id, similarity, row)
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
        scored = self._scored(LEDGER, vector, _all_of(clauses))
        scored.sort(
            key=lambda pair: (
                -pair[0],
                -int(pair[1].metadata["resolved_ts"]),
                pair[1].id,
            )
        )
        return [
            Hit(id=row.id, similarity=similarity, document=row.document, metadata=row.metadata)
            for similarity, row in scored[:n]
        ]

    def _scored(
        self, name: str, vector: np.ndarray, where: dict[str, Any]
    ) -> list[tuple[float, Row]]:
        """Every row of ``name`` that passes ``where``, with its cosine similarity to ``vector``.

        The filter runs inside the read. See the module docstring for why the
        rows are ranked here and not by Chroma's nearest-neighbour search. The
        similarity is kept to ``_TIE_DECIMALS``: stored vectors are 32-bit
        floats, so what lies beyond is noise that differs with the machine's
        arithmetic, and a recorded trace must not depend on it.
        """
        rows: list[Row] = []
        while True:  # in pages: one read of more than about 32,000 rows fails inside Chroma
            page = _rows(
                self._collections[name].get(
                    where=where,
                    include=["metadatas", "documents", "embeddings"],
                    limit=_PAGE,
                    offset=len(rows),
                )
            )
            rows.extend(page)
            if len(page) < _PAGE:
                break
        if not rows:
            return []
        matrix = np.asarray([row.vector for row in rows], dtype=np.float64)
        query = np.asarray(vector, dtype=np.float64)
        norms = np.linalg.norm(matrix, axis=1) * float(np.linalg.norm(query))
        dots = matrix @ query
        return [
            (round(float(dots[i] / norms[i]), _TIE_DECIMALS) if norms[i] > 0 else 0.0, row)
            for i, row in enumerate(rows)
        ]


_TIE_DECIMALS = 6
"""Similarities are kept to this many decimals, and equal ones are a tie."""

_PAGE = 10_000
"""Rows per read. Chroma's ``get`` fails above 32,766 rows (SQLite's limit on variables)."""

_CLOSE_UNKNOWN = 2**62
"""Sorts an event row that does not carry its event's close time after every row that does."""


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
