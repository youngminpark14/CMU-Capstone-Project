"""Ranking: turn per-query candidates into the few passages a role reads.

The default variant (``recency_origin``) is the design from the checkpoints:

1. Drop candidates below the similarity floor. This comes first, so a weak
   copy can never stand in for, and then remove, a copy that qualifies.
2. Merge duplicates. The same passage found by several queries is one
   candidate; so are near-duplicate passages of the same origin (a syndicated
   copy of a paragraph). The best-scoring copy is kept.
3. Score = similarity x recency, recency = ``0.5 ** (age_days / half_life_days)``.
4. Keep at most ``max_per_origin`` passages per origin, so one source cannot
   fill the context by repetition.
5. Return the top ``k``.

The ``similarity`` variant is the ablation: no recency factor and no
per-origin limit; the floor and merging still apply. Ties are broken by passage
id, so the same candidates always give the same ranking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import numpy as np

from pmfa.knowledge.origins import normalised_text
from pmfa.records.evidence import Passage, RetrievedPassage

RankingMode = Literal["recency_origin", "similarity"]


@dataclass(frozen=True)
class Candidate:
    """One passage as one query found it."""

    passage: Passage
    similarity: float
    query: str
    vector: np.ndarray | None = None
    """The passage vector, for recognising near-duplicate passages."""


def recency_factor(published_at: datetime, as_of: datetime, half_life_days: float) -> float:
    """``0.5 ** (age / half-life)``: 1 for a passage published at the as-of time."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    age_days = max(0.0, (as_of - published_at).total_seconds() / 86_400)
    return float(0.5 ** (age_days / half_life_days))


def _near_duplicates(a: Candidate, b: Candidate, threshold: float) -> bool:
    if a.passage.origin_id != b.passage.origin_id:
        return False
    if normalised_text(a.passage.text) == normalised_text(b.passage.text):
        return True
    if a.vector is None or b.vector is None:
        return False
    return float(np.dot(a.vector, b.vector)) >= threshold


def rank(
    candidates: Sequence[Candidate],
    *,
    as_of: datetime,
    half_life_days: float,
    k: int = 8,
    max_per_origin: int = 3,
    similarity_floor: float = 0.05,
    near_duplicate_similarity: float = 0.92,
    mode: RankingMode = "recency_origin",
) -> list[RetrievedPassage]:
    """Filter, merge, score and cut ``candidates`` to the top ``k``."""
    use_recency = mode == "recency_origin"

    def scored(c: Candidate) -> tuple[float, float]:
        recency = (
            recency_factor(c.passage.published_at, as_of, half_life_days) if use_recency else 1.0
        )
        return c.similarity * recency, recency

    # Best first, so the first copy kept of any duplicate group is its best one.
    qualifying = [c for c in candidates if c.similarity >= similarity_floor]
    ordered = sorted(qualifying, key=lambda c: (-scored(c)[0], -c.similarity, c.passage.passage_id))
    kept: list[Candidate] = []
    seen_ids: set[str] = set()
    for candidate in ordered:
        if candidate.passage.passage_id in seen_ids:
            continue
        if any(_near_duplicates(candidate, other, near_duplicate_similarity) for other in kept):
            seen_ids.add(candidate.passage.passage_id)
            continue
        seen_ids.add(candidate.passage.passage_id)
        kept.append(candidate)

    results: list[RetrievedPassage] = []
    per_origin: dict[str, int] = {}
    for candidate in kept:
        origin = candidate.passage.origin_id
        if use_recency and per_origin.get(origin, 0) >= max_per_origin:
            continue
        per_origin[origin] = per_origin.get(origin, 0) + 1
        score, recency = scored(candidate)
        results.append(
            RetrievedPassage(
                passage=candidate.passage,
                similarity=candidate.similarity,
                recency=recency,
                score=score,
                query=candidate.query,
            )
        )
        if len(results) >= k:
            break
    return results


__all__ = ["Candidate", "RankingMode", "rank", "recency_factor"]
