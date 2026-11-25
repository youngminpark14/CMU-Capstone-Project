"""Ranking on hand-built candidates: the similarity floor applies before duplicates merge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmfa.knowledge.ranking import Candidate, rank
from pmfa.records.evidence import Passage

AS_OF = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
TEXT = "The Committee decided to maintain the target range for the federal funds rate."


def passage(doc_id: str, published_at: datetime) -> Passage:
    return Passage(
        passage_id=f"{doc_id}#0",
        doc_id=doc_id,
        origin_id="o-fed",
        source="federalreserve.gov",
        url=f"https://www.federalreserve.gov/{doc_id}",
        title=doc_id,
        published_at=published_at,
        index=0,
        text=TEXT,
    )


def test_a_below_floor_copy_does_not_remove_an_above_floor_passage() -> None:
    # Two copies of the same text from one origin. The fresh copy scores higher on
    # similarity x recency but sits below the floor; the older copy qualifies alone.
    fresh = Candidate(passage("a", AS_OF - timedelta(days=1)), similarity=0.045, query="q")
    older = Candidate(passage("b", AS_OF - timedelta(days=30)), similarity=0.08, query="q")
    kwargs = {"as_of": AS_OF, "half_life_days": 30.0, "similarity_floor": 0.05}

    assert [h.passage.passage_id for h in rank([older], **kwargs)] == ["b#0"]
    assert [h.passage.passage_id for h in rank([fresh, older], **kwargs)] == ["b#0"]


def test_near_duplicates_above_the_floor_still_merge_to_the_best_copy() -> None:
    fresh = Candidate(passage("a", AS_OF - timedelta(days=1)), similarity=0.30, query="q")
    older = Candidate(passage("b", AS_OF - timedelta(days=30)), similarity=0.31, query="q")
    hits = rank([older, fresh], as_of=AS_OF, half_life_days=30.0)
    assert [h.passage.passage_id for h in hits] == ["a#0"]
