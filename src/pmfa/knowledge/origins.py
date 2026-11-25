"""Origin resolution: syndicated and copied documents count as one source.

An origin is a publisher's original document after duplicates are merged. Two
documents are the same origin only when one is substantially a copy of the
other's *text*: their normalised texts are identical, or the word shingles of
the smaller body are almost all found in the larger one (``copy_overlap`` at or
above ``origin_copy_overlap``). Shared topic or shared wording is not enough:
successive official releases are built on one template and share most of their
boilerplate, yet each is its own document with its own numbers, so a document
vector (or any looser measure) would wrongly merge the August CPI release into
the June one. A copy is also never much smaller than what it copies; a document
that quotes another in full, as FOMC minutes quote the statement, is not a copy
of it, so the overlap of two documents of very different sizes is zero.

A group of copies is one origin: its earliest-published document. Documents are
resolved in ``(published_at, doc_id)`` order and each takes the origin of the
earliest earlier document it copies, so a document's origin depends only on
documents published no later than itself, and never on the order in which they
were ingested. That keeps origin resolution free of look-ahead: whatever is
visible at an as-of time resolves the same way it would have at that time.
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

SHINGLE_WORDS = 5
"""Words per shingle: long enough that ordinary phrasing rarely repeats by chance."""

MIN_COPY_SIZE_RATIO = 0.5
"""A copy keeps at least half of what it copies; below that it is an excerpt or a quotation."""

_SKETCH_MODULUS = 16
"""One shingle in this many forms the sketch used to skip pairs that cannot be copies."""

_SKETCH_SLACK = 0.15
"""How far a sketch overlap may fall below the threshold before the exact count is skipped."""

_SKETCH_MIN = 64
"""Below this many sketch shingles the sketch is too noisy to skip on; count exactly."""

_PRIME = np.uint64(1_099_511_628_211)


def normalised_text(text: str) -> str:
    """Lower case, accents folded, punctuation and spacing collapsed."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_WORD.sub(" ", folded.lower()).strip()


def text_hash(text: str) -> str:
    """Hash of the normalised text: equal hashes mean identical normalised text."""
    return hashlib.blake2b(normalised_text(text).encode("utf-8"), digest_size=16).hexdigest()


def _word_hash(word: str) -> int:
    return int.from_bytes(hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest(), "little")


@dataclass(frozen=True)
class Shingles:
    """The hashed word shingles of one normalised body, with a fixed one-in-16 sketch."""

    all: frozenset[int]
    sketch: frozenset[int]

    def __len__(self) -> int:
        return len(self.all)


def shingles(text: str, words: int = SHINGLE_WORDS) -> Shingles:
    """Hash every run of ``words`` consecutive words of the normalised text.

    A text shorter than ``words`` is one shingle. Hashes are stable across
    processes (no Python ``hash``), so resolution is the same on every run.
    """
    tokens = normalised_text(text).split()
    if not tokens:
        return Shingles(frozenset(), frozenset())
    cache: dict[str, int] = {}
    ids = np.array([cache.setdefault(t, _word_hash(t)) for t in tokens], dtype=np.uint64)
    width = min(words, len(ids))
    count = len(ids) - width + 1
    rolled = np.zeros(count, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for offset in range(width):
            rolled = rolled * _PRIME + ids[offset : offset + count]
    values = np.unique(rolled)
    sketch = values[values % np.uint64(_SKETCH_MODULUS) == 0]
    return Shingles(frozenset(values.tolist()), frozenset(sketch.tolist()))


def _containment(small: frozenset[int], large: frozenset[int]) -> float:
    return len(small & large) / len(small) if small else 0.0


def copy_overlap(a: Shingles, b: Shingles) -> float:
    """The share of the smaller body's shingles found in the larger one; 0 for an excerpt.

    It is 1 for a verbatim copy, a copy with a line added, and a copy with its
    last paragraph trimmed; each edited word lowers it by up to
    ``SHINGLE_WORDS`` shingles. Two documents whose sizes differ by more than
    ``MIN_COPY_SIZE_RATIO`` score 0, because the smaller one is at most an
    excerpt of the larger.
    """
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    if not small or len(small) < MIN_COPY_SIZE_RATIO * len(large):
        return 0.0
    return _containment(small.all, large.all)


def _is_copy(a: Shingles, b: Shingles, threshold: float) -> bool:
    """``copy_overlap(a, b) >= threshold``, skipping the exact count when the sketch rules it out."""
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    if not small or len(small) < MIN_COPY_SIZE_RATIO * len(large):
        return False
    sketchy = len(small.sketch) >= _SKETCH_MIN
    if sketchy and _containment(small.sketch, large.sketch) < threshold - _SKETCH_SLACK:
        return False
    return _containment(small.all, large.all) >= threshold


@dataclass(frozen=True)
class DocumentSignature:
    """What origin resolution needs to know about one document."""

    doc_id: str
    published_at: datetime
    text_hash: str
    """``text_hash`` of the title and body: equal for copies identical after normalising."""
    shingles: Shingles
    """The body's word shingles."""


def resolve_origins(documents: Iterable[DocumentSignature], *, threshold: float) -> dict[str, str]:
    """Map every ``doc_id`` to the ``doc_id`` of its origin.

    Documents are taken in ``(published_at, doc_id)`` order. Each one joins the
    origin of the earliest earlier document it is a copy of (identical
    normalised text, or ``copy_overlap`` at least ``threshold``), or starts its
    own. The cost is one sketch comparison per pair of similar-sized documents
    and an exact count only for pairs whose sketches nearly match, which keeps a
    few hundred documents well under a second.
    """
    ordered = sorted(documents, key=lambda d: (d.published_at, d.doc_id))
    origin: dict[str, str] = {}
    first_by_hash: dict[str, str] = {}
    for i, doc in enumerate(ordered):
        match: str | None = first_by_hash.get(doc.text_hash)
        if match is None:
            for earlier in ordered[:i]:
                if _is_copy(earlier.shingles, doc.shingles, threshold):
                    match = earlier.doc_id
                    break
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
    "MIN_COPY_SIZE_RATIO",
    "SHINGLE_WORDS",
    "DocumentSignature",
    "Shingles",
    "copy_overlap",
    "half_life_days",
    "normalised_text",
    "primary_sources",
    "resolve_origins",
    "shingles",
    "text_hash",
]
