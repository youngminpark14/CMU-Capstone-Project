"""Chunking: documents become passages a retriever can rank and a role can cite.

A passage is about ``passage_words`` words with ``overlap_words`` of overlap with
the passage before it, so a sentence cut at a boundary still appears whole in one
of the two. The headline and the first paragraph always stay together in the
first passage: the lede is where releases and articles state their news, and
splitting it from its headline would leave a passage that says "rates" without
saying whose. Passage ids are ``"{doc_id}#{n}"``, so re-chunking the same
document gives the same ids and re-indexing is idempotent.
"""

from __future__ import annotations

import re

from pmfa.records.evidence import Document, Passage

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> list[list[str]]:
    """The document body as paragraphs of words, empty paragraphs dropped."""
    paragraphs = (p.split() for p in _PARAGRAPH_BREAK.split(text))
    return [p for p in paragraphs if p]


def passage_spans(
    first_paragraph_words: int, total_words: int, *, passage_words: int, overlap_words: int
) -> list[tuple[int, int]]:
    """Word spans ``[start, end)`` of a body's passages.

    The first span covers the whole first paragraph and at least ``passage_words``
    words. A first paragraph longer than two passages (text with no paragraph
    breaks, say) is not kept whole: the first span then holds ``passage_words``
    words, so one run-on document cannot become one giant passage. Every later
    span starts ``overlap_words`` before the previous one ends.
    """
    if passage_words <= 0:
        raise ValueError("passage_words must be positive")
    if not 0 <= overlap_words < passage_words:
        raise ValueError("overlap_words must be in [0, passage_words)")
    if total_words == 0:
        return [(0, 0)]
    if first_paragraph_words <= 2 * passage_words:
        first_end = max(passage_words, first_paragraph_words)
    else:
        first_end = passage_words
    spans = [(0, min(first_end, total_words))]
    while spans[-1][1] < total_words:
        start = spans[-1][1] - overlap_words
        spans.append((start, min(start + passage_words, total_words)))
    return spans


def chunk_document(
    document: Document,
    *,
    origin_id: str,
    passage_words: int = 250,
    overlap_words: int = 50,
) -> list[Passage]:
    """Split one document into passages tagged with its source, URL, title, publish time and origin.

    The first passage starts with the headline. Word counts refer to the body;
    the headline rides along on top of the first passage.
    """
    paragraphs = _paragraphs(document.text)
    words = [w for p in paragraphs for w in p]
    first_paragraph = len(paragraphs[0]) if paragraphs else 0
    spans = passage_spans(
        first_paragraph, len(words), passage_words=passage_words, overlap_words=overlap_words
    )
    headline = " ".join(document.title.split())
    passages: list[Passage] = []
    for n, (start, end) in enumerate(spans):
        body = " ".join(words[start:end])
        text = f"{headline}\n\n{body}".strip() if n == 0 and headline else body
        passages.append(
            Passage(
                passage_id=passage_id(document.doc_id, n),
                doc_id=document.doc_id,
                origin_id=origin_id,
                source=document.source,
                url=document.url,
                title=document.title,
                published_at=document.published_at,
                index=n,
                text=text,
            )
        )
    return passages


def passage_id(doc_id: str, n: int) -> str:
    """The stable id of a document's ``n``-th passage."""
    return f"{doc_id}#{n}"


__all__ = ["chunk_document", "passage_id", "passage_spans"]
