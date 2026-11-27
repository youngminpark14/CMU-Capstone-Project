"""The worked example of recency against similarity, on real passages and without a data directory.

The fixture holds the nine Federal Reserve passages that make up the two
top-five lists of ``scripts/recency_example.py`` on the frozen corpus. The
test embeds them and the query with the default embedder, ranks them both
ways with the production ranking, and checks what was decided: by similarity
alone the old July statement leads, and with the recency factor the newest
FOMC document, the September minutes, takes its place. It also pins the
fixture to the embedder and the table in ``docs/architecture.md`` to the
computation, so neither can drift.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from pmfa.config import Config
from pmfa.knowledge.embedders import make_embedder
from pmfa.knowledge.ranking import Candidate, RankingMode, rank
from pmfa.records.evidence import Passage, RetrievedPassage

FIXTURE = Path(__file__).parent / "fixtures" / "recency_example.json"
ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"
SECTION = "Worked example: recency against similarity"

OLD_STATEMENT = "fed:monetary20260729a#0"
NEWEST_RELEASE = "fed:fomcminutes20260916"
"""The minutes of the September meeting: the newest FOMC document at the as-of time."""

SHOWN = 5
ROW_RANKING = {"Similarity alone": "similarity", "With recency": "recency_origin"}


@pytest.fixture(scope="module")
def example() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def computed(example: dict[str, Any]) -> dict[str, Any]:
    """The fixture's passages embedded with the default embedder and ranked both ways."""
    retrieval = Config().retrieval
    assert example["embedder"] == retrieval.embedder
    embedder = make_embedder(retrieval.embedder)
    rows = example["passages"]
    vectors = embedder.embed([example["query"], *[row["text"] for row in rows]])
    query, passage_vectors = vectors[0], vectors[1:]
    as_of = datetime.fromisoformat(example["as_of"])
    candidates = [
        Candidate(
            passage=Passage.model_validate(
                {k: v for k, v in row.items() if k in Passage.model_fields}
            ),
            similarity=float(query @ vector),
            query=example["query"],
            vector=vector,
        )
        for row, vector in zip(rows, passage_vectors, strict=True)
    ]

    def ranked(mode: RankingMode) -> list[RetrievedPassage]:
        return rank(
            candidates,
            as_of=as_of,
            half_life_days=example["half_life_days"],
            k=retrieval.top_k,
            max_per_origin=retrieval.max_per_origin,
            similarity_floor=retrieval.similarity_floor,
            near_duplicate_similarity=retrieval.near_duplicate_similarity,
            mode=mode,
        )

    return {
        "as_of": as_of,
        "similarity": {c.passage.passage_id: c.similarity for c in candidates},
        "labels": {row["passage_id"]: row["label"] for row in rows},
        "similarity_only": ranked("similarity"),
        "recency_origin": ranked("recency_origin"),
    }


def _ids(hits: list[RetrievedPassage]) -> list[str]:
    return [h.passage.passage_id for h in hits]


def test_the_fixture_similarities_are_what_the_embedder_computes(
    example: dict[str, Any], computed: dict[str, Any]
) -> None:
    for row in example["passages"]:
        assert computed["similarity"][row["passage_id"]] == pytest.approx(
            row["similarity"], abs=5e-4
        ), row["passage_id"]


def test_by_similarity_alone_the_old_statement_leads(computed: dict[str, Any]) -> None:
    ids = _ids(computed["similarity_only"])
    assert ids[0] == OLD_STATEMENT
    newest = [i for i in ids if i.startswith(NEWEST_RELEASE + "#")]
    assert newest, "the newest release is retrieved"
    assert all(ids.index(i) > 0 for i in newest)


def test_with_recency_the_newest_release_leads_and_the_old_statement_falls_below_it(
    computed: dict[str, Any],
) -> None:
    ids = _ids(computed["recency_origin"])
    assert ids[0].startswith(NEWEST_RELEASE + "#")
    newest = [n for n, i in enumerate(ids) if i.startswith(NEWEST_RELEASE + "#")]
    old = ids.index(OLD_STATEMENT) if OLD_STATEMENT in ids else len(ids)
    assert old > max(newest)


def _table_rows() -> list[dict[str, str]]:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(SECTION)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"docs/architecture.md has no '## {SECTION}' section"
    lines = [line for line in match.group(1).splitlines() if line.startswith("|")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return [
        dict(zip(header, (cell.strip() for cell in line.strip("|").split("|")), strict=True))
        for line in lines[2:]
    ]


def test_the_table_in_the_architecture_page_equals_the_computed_rankings(
    computed: dict[str, Any],
) -> None:
    rows = _table_rows()
    assert {row["Ranking"] for row in rows} == set(ROW_RANKING)
    as_of: datetime = computed["as_of"]
    for label, mode in ROW_RANKING.items():
        table = [row for row in rows if row["Ranking"] == label]
        hits = computed["similarity_only" if mode == "similarity" else mode][:SHOWN]
        assert len(table) == len(hits) == SHOWN
        for n, (row, hit) in enumerate(zip(table, hits, strict=True), start=1):
            p = hit.passage
            age = (as_of - p.published_at).total_seconds() / 86_400
            assert row["Rank"] == str(n)
            assert row["Passage"] == computed["labels"][p.passage_id]
            assert row["Published"] == f"{p.published_at:%Y-%m-%d}"
            assert row["Age (days)"] == f"{age:.1f}"
            assert row["Similarity"] == f"{hit.similarity:.3f}"
            expected_factor = "not applied" if mode == "similarity" else f"{hit.recency:.3f}"
            assert row["Recency factor"] == expected_factor
            assert row["Score"] == f"{hit.score:.3f}"
