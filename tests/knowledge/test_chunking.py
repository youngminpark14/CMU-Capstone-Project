from __future__ import annotations

from itertools import pairwise

from pmfa.knowledge.chunking import chunk_document, passage_spans
from tests.knowledge.conftest import AS_OF, FED_STATEMENT, doc


def numbered_words(start: int, count: int) -> str:
    return " ".join(f"w{i}" for i in range(start, start + count))


def body_words(text: str, title: str | None = None) -> list[str]:
    if title is not None:
        assert text.startswith(title + "\n\n")
        text = text[len(title) + 2 :]
    return text.split()


def test_short_document_is_one_passage_with_headline_first() -> None:
    d = doc("fomc", FED_STATEMENT, title="Federal Reserve issues FOMC statement")
    [p] = chunk_document(d, origin_id="fomc")
    assert p.passage_id == "fomc#0" and p.index == 0
    assert p.text.startswith("Federal Reserve issues FOMC statement\n\nRecent indicators")
    assert body_words(p.text, d.title) == FED_STATEMENT.split()


def test_passages_are_250_words_with_50_word_overlap() -> None:
    paragraphs = [numbered_words(0, 40), numbered_words(40, 300), numbered_words(340, 360)]
    d = doc("long", "\n\n".join(paragraphs), title="Headline")
    passages = chunk_document(d, origin_id="origin-x", passage_words=250, overlap_words=50)
    bodies = [body_words(p.text, "Headline" if p.index == 0 else None) for p in passages]

    assert [p.passage_id for p in passages] == [f"long#{n}" for n in range(len(passages))]
    assert all(len(b) == 250 for b in bodies[:-1])
    assert 0 < len(bodies[-1]) <= 250
    for before, after in pairwise(bodies):
        assert before[-50:] == after[:50]
    covered = {w for b in bodies for w in b}
    assert covered == {f"w{i}" for i in range(700)}
    for p in passages:
        assert (p.source, p.url, p.title, p.published_at, p.origin_id) == (
            d.source,
            d.url,
            d.title,
            d.published_at,
            "origin-x",
        )


def test_headline_and_long_first_paragraph_stay_together() -> None:
    first = numbered_words(0, 320)
    d = doc("lede", first + "\n\n" + numbered_words(320, 200), title="Big news")
    passages = chunk_document(d, origin_id="lede")
    first_body = body_words(passages[0].text, "Big news")
    assert first_body == first.split()
    assert body_words(passages[1].text)[:50] == first.split()[-50:]


def test_run_on_text_without_paragraphs_is_still_split() -> None:
    d = doc("blob", numbered_words(0, 1200), title="Blob")
    passages = chunk_document(d, origin_id="blob")
    assert len(body_words(passages[0].text, "Blob")) == 250
    assert len(passages) == 6


def test_chunking_is_stable() -> None:
    d = doc("x", "\n\n".join([numbered_words(0, 90), numbered_words(90, 500)]), published_at=AS_OF)
    assert chunk_document(d, origin_id="x") == chunk_document(d, origin_id="x")


def test_spans_cover_the_body_exactly() -> None:
    assert passage_spans(0, 0, passage_words=250, overlap_words=50) == [(0, 0)]
    assert passage_spans(10, 250, passage_words=250, overlap_words=50) == [(0, 250)]
    assert passage_spans(10, 251, passage_words=250, overlap_words=50) == [(0, 250), (200, 251)]
