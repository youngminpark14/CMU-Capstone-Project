"""Origin resolution: copies of one text are one origin; documents sharing a template are not."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from pmfa.knowledge.embedders import HashingEmbedder
from pmfa.knowledge.origins import (
    DocumentSignature,
    copy_overlap,
    resolve_origins,
    shingles,
    text_hash,
)
from pmfa.knowledge.retriever import ChromaKnowledge
from pmfa.records.evidence import Document
from tests.knowledge.conftest import AS_OF, FED_STATEMENT, WIRE_ARTICLE, config, doc

# Two invented monthly releases on one template: the boilerplate is word for word
# the same, the month, the numbers and the lede differ. Like real successive
# releases, most of the text is shared.
BOILERPLATE = """This news release presents statistics from the monthly survey of retail prices. The survey covers food, energy, shelter, apparel, transportation, medical care, recreation, education and communication, and other goods and services purchased by urban households.

Seasonal adjustment removes the effect of price changes that normally occur at the same time and in about the same magnitude every year, such as price movements resulting from changing climatic conditions, production cycles, model changeovers, and holidays. Seasonal factors are revised each year with the release of January data and five years of seasonally adjusted data are recalculated.

Index data are collected in seventy five urban areas across the country and from about twenty three thousand retail and service establishments. Data on rents are collected from about fifty thousand landlords or tenants. All taxes directly associated with the purchase and use of items are included in the index.

The sampling error of the monthly change in the all items index is about one tenth of a percentage point. Users should consider this when interpreting small month to month movements. Detailed reliability statistics are published on the agency website along with the full set of tables.

Information in this release will be made available to sensory impaired individuals upon request. The release is in the public domain and may be reproduced without permission; the agency asks that it be credited as the source."""

RELEASE_LEDE = """The retail price index increased {monthly} percent on a seasonally adjusted basis in {month}, after {previous} in {prior}. Over the last 12 months, the all items index increased {annual} percent before seasonal adjustment. The index for shelter rose {shelter} percent and the energy index {energy}."""


def release(month: str, prior: str, **numbers: str) -> str:
    return RELEASE_LEDE.format(month=month, prior=prior, **numbers) + "\n\n" + BOILERPLATE


JULY = release(
    "July", "June", monthly="0.2", previous="rising 0.3 percent", annual="2.7", shelter="0.3",
    energy="fell 1.1 percent",
)  # fmt: skip
AUGUST = release(
    "August", "July", monthly="0.4", previous="rising 0.2 percent", annual="2.9", shelter="0.4",
    energy="rose 0.7 percent",
)  # fmt: skip


def syndicated() -> list[Document]:
    """The wire story run twice more: once verbatim, once with a new headline and its last paragraph cut."""
    paragraphs = WIRE_ARTICLE.split("\n\n")
    return [
        doc(
            "wire-fed-cut",
            WIRE_ARTICLE,
            title="Fed officials signal openness to a December rate cut",
            published_at=datetime(2026, 11, 12, 14, 0, tzinfo=UTC),
        ),
        doc(
            "portal-fed-cut",
            WIRE_ARTICLE,
            title="Fed officials signal openness to a December rate cut",
            source="portal.example",
            published_at=datetime(2026, 11, 12, 15, 0, tzinfo=UTC),
        ),
        doc(
            "gazette-fed-cut",
            "\n\n".join(paragraphs[:-1]),
            title="Rate cut in December back on the table, Fed officials say",
            source="gazette.example",
            published_at=datetime(2026, 11, 12, 19, 0, tzinfo=UTC),
        ),
    ]


def releases() -> list[Document]:
    return [
        doc(
            "rpi-2026-07",
            JULY,
            title="Retail Price Index for July 2026",
            source="bls.gov",
            published_at=datetime(2026, 8, 12, 12, 30, tzinfo=UTC),
            kind="cpi_release",
        ),
        doc(
            "rpi-2026-08",
            AUGUST,
            title="Retail Price Index for August 2026",
            source="bls.gov",
            published_at=datetime(2026, 9, 11, 12, 30, tzinfo=UTC),
            kind="cpi_release",
        ),
    ]


def signature(d: Document) -> DocumentSignature:
    return DocumentSignature(
        doc_id=d.doc_id,
        published_at=d.published_at,
        text_hash=text_hash(f"{d.title}\n{d.text}"),
        shingles=shingles(d.text),
    )


def stored_origins(knowledge: ChromaKnowledge) -> dict[str, str]:
    return {row.id: row.metadata["origin_id"] for row in knowledge.index.all_documents()}


# -- the measure


def test_a_copy_overlaps_fully_and_a_template_sibling_does_not() -> None:
    wire, portal, gazette = (shingles(d.text) for d in syndicated())
    assert copy_overlap(wire, portal) == 1.0
    assert copy_overlap(wire, gazette) == 1.0
    edited = shingles(WIRE_ARTICLE.replace("Wednesday", "Thursday"))
    assert copy_overlap(wire, edited) > config().retrieval.origin_copy_overlap

    overlap = copy_overlap(shingles(JULY), shingles(AUGUST))
    assert 0.7 < overlap < config().retrieval.origin_copy_overlap


def test_a_document_quoting_another_in_full_is_not_its_copy() -> None:
    minutes = (
        "Minutes of the meeting. Participants discussed the outlook at length.\n\n"
        + FED_STATEMENT
        + "\n\n"
        + WIRE_ARTICLE * 3
    )
    assert copy_overlap(shingles(FED_STATEMENT), shingles(minutes)) == 0.0


# -- resolution


def test_syndicated_twice_is_one_origin(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents(syndicated())
    assert set(stored_origins(knowledge).values()) == {"wire-fed-cut"}

    passages = knowledge.get_passages(
        ["wire-fed-cut#0", "portal-fed-cut#0", "gazette-fed-cut#0"], as_of=AS_OF
    )
    [origin] = knowledge.origins(passages, category="fed_decision")
    assert origin.origin_id == "wire-fed-cut"
    assert origin.url == "https://newswire.example/wire-fed-cut"
    assert origin.published_at == datetime(2026, 11, 12, 14, 0, tzinfo=UTC)


def test_two_releases_on_one_template_are_two_origins(knowledge: ChromaKnowledge) -> None:
    july, august = releases()
    knowledge.index_documents([july, august])
    assert stored_origins(knowledge) == {"rpi-2026-07": "rpi-2026-07", "rpi-2026-08": "rpi-2026-08"}

    [passage] = knowledge.get_passages(["rpi-2026-08#0"], as_of=AS_OF)
    [origin] = knowledge.origins([passage], category="cpi")
    assert (origin.origin_id, origin.url) == ("rpi-2026-08", "https://bls.gov/rpi-2026-08")
    assert origin.published_at == august.published_at

    # Their document vectors are as close as copies would be: the old rule merged them.
    vectors = knowledge.index.embed([july.text, august.text])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    assert float(vectors[0] @ vectors[1]) >= config().retrieval.near_duplicate_similarity


def test_the_earliest_copy_is_the_origin_whatever_the_ingestion_order() -> None:
    documents = [*syndicated(), *releases()]
    expected = {
        "wire-fed-cut": "wire-fed-cut",
        "portal-fed-cut": "wire-fed-cut",
        "gazette-fed-cut": "wire-fed-cut",
        "rpi-2026-07": "rpi-2026-07",
        "rpi-2026-08": "rpi-2026-08",
    }
    threshold = config().retrieval.origin_copy_overlap
    assert resolve_origins(map(signature, documents), threshold=threshold) == expected
    assert resolve_origins(map(signature, documents[::-1]), threshold=threshold) == expected

    with ChromaKnowledge(config(), embedder=HashingEmbedder()) as knowledge:
        knowledge.index_documents(documents[::-1][:2])
        knowledge.index_documents(documents[::-1][2:])
        assert stored_origins(knowledge) == expected
        [late] = knowledge.get_passages(["gazette-fed-cut#0"], as_of=AS_OF)
        assert late.origin_id == "wire-fed-cut"


def test_a_reopened_index_still_recognises_copies(tmp_path) -> None:
    original, *copies = syndicated()
    with ChromaKnowledge(config(), path=tmp_path, embedder=HashingEmbedder()) as knowledge:
        knowledge.index_documents([original, *releases()])
    with ChromaKnowledge(config(), path=tmp_path, embedder=HashingEmbedder()) as knowledge:
        knowledge.index_documents(copies)
        origins = stored_origins(knowledge)
    assert origins["portal-fed-cut"] == origins["gazette-fed-cut"] == "wire-fed-cut"
    assert origins["rpi-2026-08"] == "rpi-2026-08"
