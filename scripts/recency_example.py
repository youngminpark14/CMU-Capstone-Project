"""Recency against similarity: the two evidence rankings side by side on the frozen corpus.

Retrieval ranks a passage by its similarity to the query times a recency
factor, ``0.5 ** (age in days / half-life)``, and keeps at most a few passages
per origin (``pmfa.knowledge.ranking``). The ``similarity`` variant is the
ablation without the factor and without the per-origin limit. This script runs
both variants for one fixed question about the Federal Reserve, as of two days
before the October 2026 decision, and prints the top five of each, so the
effect of the recency factor can be seen on real text rather than on made-up
passages. It is an illustration of the mechanism on one query and one date,
not a measurement of its value: that is the ranking ablation of the backtest
(``single_pass`` against ``single_pass_similarity`` in ``docs/evaluation.md``).

It goes through the project's own code paths: ``BacktestData`` and its
retrieval index, the canary guard, and ``with_ranking``, as the backtest's
baselines do. The index is copied to a temporary directory first, so the
script reads the data and writes nothing into the repository (``BacktestData``
builds the index under ``data/index`` first if it is missing, as a backtest
would).

Run it after ``pmfa data build``::

    uv run python scripts/recency_example.py

``docs/architecture.md`` quotes the output under "Worked example: recency
against similarity", and ``tests/knowledge/test_recency_example.py`` checks
those numbers against a fixture of the passages involved.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pmfa.config import Config, load_config
from pmfa.knowledge.origins import half_life_days
from pmfa.knowledge.ranking import RankingMode, recency_factor
from pmfa.world.backtest import BacktestData, backtest_config

QUERY = "Will the Federal Reserve raise the federal funds target range at its next meeting?"
CATEGORY = "fed_decision"
"""The category of the followed Fed decision series; it sets the recency half-life."""
AS_OF = datetime(2026, 10, 26, tzinfo=UTC)
"""Two days before the decision of the October 27-28, 2026 meeting."""
SHOWN = 5
VARIANTS: tuple[RankingMode, ...] = ("similarity", "recency_origin")


@dataclass(frozen=True)
class Row:
    """One ranked passage as the script prints it."""

    rank: int
    passage_id: str
    title: str
    published_at: datetime
    age_days: float
    similarity: float
    recency: float
    score: float


def measure(config: Config) -> dict[str, list[Row]]:
    """The top ``SHOWN`` passages of each ranking variant for ``QUERY`` as of ``AS_OF``.

    Each variant retrieves the top ``retrieval.top_k`` passages, as an episode
    does, and the first ``SHOWN`` of them are kept.
    """
    data = BacktestData(config)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = data.knowledge(Path(tmp) / "index")
            try:
                guard = data.guard(knowledge)
                ranked: dict[str, list[Row]] = {}
                for variant in VARIANTS:
                    hits = guard.with_ranking(variant).search_evidence(
                        [QUERY], as_of=AS_OF, category=CATEGORY, k=config.retrieval.top_k
                    )
                    ranked[variant] = [
                        Row(
                            rank=n,
                            passage_id=h.passage.passage_id,
                            title=h.passage.title,
                            published_at=h.passage.published_at,
                            age_days=(AS_OF - h.passage.published_at).total_seconds() / 86_400,
                            similarity=h.similarity,
                            recency=h.recency,
                            score=h.score,
                        )
                        for n, h in enumerate(hits[:SHOWN], start=1)
                    ]
                return ranked
            finally:
                knowledge.close()
    finally:
        data.close()


def _table(rows: list[Row]) -> list[str]:
    lines = [
        f"  {'rank':>4}  {'passage':<30}  {'published':<10}  {'age d':>6}  "
        f"{'sim':>6}  {'factor':>6}  {'score':>6}  title"
    ]
    for r in rows:
        lines.append(
            f"  {r.rank:>4}  {r.passage_id:<30}  {r.published_at:%Y-%m-%d}  {r.age_days:>6.1f}  "
            f"{r.similarity:>6.3f}  {r.recency:>6.3f}  {r.score:>6.3f}  {r.title}"
        )
    return lines


def _what_changed(ranked: dict[str, list[Row]], half_life: float) -> list[str]:
    """One or two plain sentences: what moved to the top, and what older text dropped out."""
    by_similarity, by_recency = ranked["similarity"], ranked["recency_origin"]
    if not by_similarity or not by_recency:
        return ["Nothing was retrieved, so there is nothing to compare."]
    old_first, new_first = by_similarity[0], by_recency[0]
    before = {r.passage_id: r.rank for r in by_similarity}
    moved = (
        f"from rank {before[new_first.passage_id]}"
        if new_first.passage_id in before
        else f"from outside the top {SHOWN}"
    )
    lines = [
        f"With the recency factor, {new_first.passage_id} ({new_first.title}, "
        f"{new_first.age_days:.1f} days old) moved {moved} to rank 1."
    ]
    kept = {r.passage_id for r in by_recency}
    dropped = [r for r in by_similarity if r.passage_id not in kept]
    if dropped:
        parts = []
        for r in dropped:
            factor = recency_factor(r.published_at, AS_OF, half_life)
            parts.append(
                f"{r.passage_id} ({r.age_days:.1f} days old, factor {factor:.3f}, "
                f"score {r.similarity * factor:.3f})"
            )
        lines.append(f"Dropped out of the top {SHOWN}: " + "; ".join(parts) + ".")
    if old_first.passage_id != new_first.passage_id and old_first.passage_id in kept:
        lines.append(f"{old_first.passage_id}, first by similarity alone, is still in the list.")
    return lines


def main() -> int:
    config = backtest_config(load_config())
    half_life = half_life_days(config, CATEGORY)
    ranked = measure(config)
    lines = [
        f"query: {QUERY}",
        f"as of: {AS_OF.isoformat()}",
        f"category: {CATEGORY}, half-life {half_life:g} days, "
        f"embedder {config.retrieval.embedder}, at most {config.retrieval.max_per_origin} "
        "passages per origin with recency",
    ]
    for variant in VARIANTS:
        title = "similarity alone" if variant == "similarity" else "similarity x recency"
        lines += ["", f"{variant} ({title}): top {SHOWN}", *_table(ranked[variant])]
    lines += ["", *_what_changed(ranked, half_life)]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
