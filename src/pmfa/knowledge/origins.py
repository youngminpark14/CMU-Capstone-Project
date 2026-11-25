"""Origin resolution: syndicated and near-duplicate copies count as one source.

Two documents are near-duplicates when the cosine similarity of their document
vectors is at least ``near_duplicate_similarity``, or when their normalised
texts are identical. A group of near-duplicates is one origin: its
earliest-published document. Documents are resolved in publish order and each
takes the origin of the earliest earlier document it duplicates, so a
document's origin depends only on documents published no later than itself.
That keeps origin resolution free of look-ahead: whatever is visible at an
as-of time resolves the same way it would have at that time.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pmfa.config import Config

_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalised_text(text: str) -> str:
    """Lower case, accents folded, punctuation and spacing collapsed."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_WORD.sub(" ", folded.lower()).strip()


def text_hash(text: str) -> str:
    """Hash of the normalised text: equal hashes mean identical normalised text."""
    return hashlib.blake2b(normalised_text(text).encode("utf-8"), digest_size=16).hexdigest()


@dataclass(frozen=True)
class DocumentSignature:
    """What origin resolution needs to know about one document."""

    doc_id: str
    published_at: datetime
    text_hash: str
    vector: np.ndarray
    """The L2-normalised document vector."""


def resolve_origins(documents: Iterable[DocumentSignature], *, threshold: float) -> dict[str, str]:
    """Map every ``doc_id`` to the ``doc_id`` of its origin.

    Documents are taken in ``(published_at, doc_id)`` order. Each one joins the
    origin of the earliest earlier document that is a near-duplicate of it, or
    starts its own.
    """
    ordered = sorted(documents, key=lambda d: (d.published_at, d.doc_id))
    origin: dict[str, str] = {}
    first_by_hash: dict[str, str] = {}
    if not ordered:
        return origin
    matrix = np.stack([d.vector for d in ordered]).astype(np.float32)
    for i, doc in enumerate(ordered):
        match: str | None = first_by_hash.get(doc.text_hash)
        if match is None and i > 0:
            similar = np.flatnonzero(matrix[:i] @ matrix[i] >= threshold)
            if similar.size:
                match = ordered[int(similar[0])].doc_id
        origin[doc.doc_id] = origin[match] if match is not None else doc.doc_id
        first_by_hash.setdefault(doc.text_hash, doc.doc_id)
    return origin


def primary_sources(config: Config, category: str) -> frozenset[str]:
    """The official publishers whose documents are primary origins for ``category``."""
    return frozenset(
        source
        for series in config.follow.series
        if series.category == category
        for source in series.primary_sources
    )


def half_life_days(config: Config, category: str, default: float = 30.0) -> float:
    """The recency half-life of ``category``: the first followed series of that category sets it."""
    for series in config.follow.series:
        if series.category == category:
            return series.recency_half_life_days
    return default


__all__ = [
    "DocumentSignature",
    "half_life_days",
    "normalised_text",
    "primary_sources",
    "resolve_origins",
    "text_hash",
]
