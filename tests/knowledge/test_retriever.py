from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.knowledge.embedders import EmbedderMismatch, HashingEmbedder
from pmfa.knowledge.retriever import ChromaKnowledge
from pmfa.ports import Knowledge
from pmfa.records.base import RunMode
from pmfa.records.episode import LedgerEntry
from pmfa.records.evidence import Document
from pmfa.records.market import Event, Market, MarketStatus
from tests.knowledge.conftest import (
    AS_OF,
    CPI_RELEASE,
    FED_SPEECH,
    FED_STATEMENT,
    WIRE_ARTICLE,
    config,
    corpus,
    doc,
)

FED_QUERIES = [
    "Federal Reserve cuts the federal funds rate in December",
    "Federal Reserve holds rates steady as inflation stays elevated",
]


def make(cfg: Config | None = None, path: Path | None = None) -> ChromaKnowledge:
    return ChromaKnowledge(cfg or config(), path=path, embedder=HashingEmbedder())


def syndicated_copies() -> list[Document]:
    """The wire story as two other outlets ran it: a reprint line and a new headline; re-punctuated."""
    return [
        doc(
            "daily-paper-fed",
            "Reprinted from Newswire.\n\n" + WIRE_ARTICLE,
            title="Fed officials signal openness to December rate cut",
            source="dailypaper.example",
            published_at=datetime(2026, 11, 12, 18, 0, tzinfo=UTC),
        ),
        doc(
            "city-news-fed",
            WIRE_ARTICLE.upper().replace(",", "").replace(".", " ."),
            title="Fed officials signal openness to a December rate cut",
            source="citynews.example",
            published_at=datetime(2026, 11, 13, 9, 0, tzinfo=UTC),
        ),
    ]


def test_implements_the_knowledge_port(knowledge: ChromaKnowledge) -> None:
    assert isinstance(knowledge, Knowledge)


# -- leakage


def test_canary_after_as_of_is_never_retrieved(knowledge: ChromaKnowledge) -> None:
    canary_text = Config().evaluation.canary_text
    canary = doc(
        "canary",
        f"{canary_text}. The Federal Reserve cut the federal funds rate by a quarter point "
        "in December and signaled more cuts ahead as inflation fell.",
        title="Fed cuts rates in December",
        source="federalreserve.gov",
        published_at=AS_OF + timedelta(seconds=1),
    )
    knowledge.index_documents([*corpus(), canary])
    queries = [canary_text, canary.text, canary.title, *FED_QUERIES]

    for query in queries:
        hits = knowledge.search_evidence([query], as_of=AS_OF, category="fed_decision", k=50)
        assert hits and all(h.passage.doc_id != "canary" for h in hits)
    assert knowledge.get_passages(["canary#0"], as_of=AS_OF) == []

    later = AS_OF + timedelta(seconds=1)
    hits = knowledge.search_evidence([canary_text], as_of=later, category="fed_decision")
    assert hits[0].passage.doc_id == "canary"  # the trap is armed


def test_fractional_second_after_as_of_is_excluded(knowledge: ChromaKnowledge) -> None:
    d = doc("edge", FED_SPEECH, published_at=AS_OF + timedelta(microseconds=1))
    knowledge.index_documents([d])
    assert knowledge.search_evidence([FED_SPEECH], as_of=AS_OF, category="fed_decision") == []
    assert knowledge.get_passages(["edge#0"], as_of=AS_OF) == []


def _found(knowledge: ChromaKnowledge, as_of: datetime) -> tuple[set[str], set[str]]:
    """The documents that a search and a lookup by passage id return at ``as_of``.

    Decision 0026: publish times are rounded up to the second and as-of times down,
    so nothing published after the as-of time can share its second and be read.
    """
    hits = knowledge.search_evidence(
        [FED_SPEECH, FED_STATEMENT], as_of=as_of, category="fed_decision", k=50
    )
    passages = knowledge.get_passages(["on-the-second#0", "later#0"], as_of=as_of)
    return {h.passage.doc_id for h in hits}, {p.doc_id for p in passages}


def test_an_as_of_time_with_a_fraction_of_a_second_reads_its_second_and_not_the_next(
    knowledge: ChromaKnowledge,
) -> None:
    knowledge.index_documents(
        [
            doc("on-the-second", FED_SPEECH, published_at=AS_OF),
            doc("later", FED_STATEMENT, published_at=AS_OF + timedelta(seconds=1)),
        ]
    )
    found = _found(knowledge, AS_OF + timedelta(milliseconds=900))
    assert found == ({"on-the-second"}, {"on-the-second"})


def test_a_passage_published_later_in_the_as_of_times_own_second_is_not_read(
    knowledge: ChromaKnowledge,
) -> None:
    knowledge.index_documents(
        [
            doc("on-the-second", FED_SPEECH, published_at=AS_OF),
            doc("later", FED_STATEMENT, published_at=AS_OF + timedelta(milliseconds=500)),
        ]
    )
    found = _found(knowledge, AS_OF + timedelta(milliseconds=200))
    assert found == ({"on-the-second"}, {"on-the-second"})


def test_published_after_keeps_only_newer_passages(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents(corpus())
    hits = knowledge.search_evidence(
        FED_QUERIES,
        as_of=AS_OF,
        category="fed_decision",
        published_after=datetime(2026, 11, 11, tzinfo=UTC),
        k=50,
    )
    assert hits
    assert all(h.passage.published_at > datetime(2026, 11, 11, tzinfo=UTC) for h in hits)


def test_naive_as_of_is_refused(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents(corpus())
    with pytest.raises(ValueError, match="naive"):
        knowledge.search_evidence(FED_QUERIES, as_of=datetime(2026, 11, 20), category="cpi")


# -- origins


def test_syndicated_article_resolves_to_one_origin(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents([*corpus(), *syndicated_copies()])
    ids = ["wire-fed-cut#0", "daily-paper-fed#0", "city-news-fed#0"]
    passages = knowledge.get_passages(ids, as_of=AS_OF)
    assert [p.passage_id for p in passages] == ids

    [origin] = knowledge.origins(passages, category="fed_decision")
    assert origin.origin_id == "wire-fed-cut"
    assert origin.source == "newswire.example"
    assert origin.published_at == datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    assert not origin.is_primary


def test_two_different_articles_are_two_origins(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents(corpus())
    passages = knowledge.get_passages(["wire-fed-cut#0", "fed-speech-2026-11#0"], as_of=AS_OF)
    origins = knowledge.origins(passages, category="fed_decision")
    assert [o.origin_id for o in origins] == ["wire-fed-cut", "fed-speech-2026-11"]
    assert [o.is_primary for o in origins] == [False, True]
    [cpi] = knowledge.origins(passages[1:], category="cpi")
    assert not cpi.is_primary


def test_original_indexed_after_its_copies_becomes_their_origin(
    knowledge: ChromaKnowledge,
) -> None:
    knowledge.index_documents(syndicated_copies())
    [before] = knowledge.get_passages(["city-news-fed#0"], as_of=AS_OF)
    assert before.origin_id == "daily-paper-fed"

    knowledge.index_documents(corpus())
    passages = knowledge.get_passages(["city-news-fed#0", "daily-paper-fed#0"], as_of=AS_OF)
    assert {p.origin_id for p in passages} == {"wire-fed-cut"}


def test_search_merges_syndicated_copies(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents([*corpus(), *syndicated_copies()])
    hits = knowledge.search_evidence(
        ["Fed officials signal a December rate cut"], as_of=AS_OF, category="fed_decision"
    )
    from_story = [h for h in hits if h.passage.origin_id == "wire-fed-cut"]
    assert len(from_story) == 1
    assert hits[0].passage.origin_id == "wire-fed-cut"


# -- ranking


def test_at_most_three_passages_per_origin() -> None:
    cfg = config(passage_words=40, passage_overlap_words=10)
    long_doc = doc(
        "fed-long",
        "\n\n".join([FED_STATEMENT, FED_SPEECH, WIRE_ARTICLE]),
        title="Federal Reserve policy review",
        source="federalreserve.gov",
    )
    with make(cfg) as k:
        k.index_documents([long_doc, *corpus()])
        hits = k.search_evidence(FED_QUERIES, as_of=AS_OF, category="fed_decision", k=8)
        per_origin = [h.passage.origin_id for h in hits].count("fed-long")
        assert per_origin == 3

    ablation = config(passage_words=40, passage_overlap_words=10, ranking="similarity")
    with make(ablation) as k:
        k.index_documents([long_doc, *corpus()])
        hits = k.search_evidence(FED_QUERIES, as_of=AS_OF, category="fed_decision", k=8)
        assert [h.passage.origin_id for h in hits].count("fed-long") > 3
        assert all(h.recency == 1.0 and h.score == h.similarity for h in hits)


def test_recency_reorders_against_similarity_only() -> None:
    query = "Federal Reserve December rate cut"
    old = doc(
        "old-cut-story",
        "The Federal Reserve weighed a December rate cut. Officials said a December rate cut "
        "at the Federal Reserve was likely.",
        published_at=AS_OF - timedelta(days=200),
    )
    new = doc(
        "new-cut-story",
        "Officials at the Federal Reserve said a cut in December remained possible, "
        "depending on the next inflation report and the labor market.",
        source="otherwire.example",
        published_at=AS_OF - timedelta(days=1),
    )
    with make(config(ranking="similarity")) as k:
        k.index_documents([old, new])
        by_similarity = k.search_evidence([query], as_of=AS_OF, category="fed_decision")
    with make() as k:
        k.index_documents([old, new])
        by_recency = k.search_evidence([query], as_of=AS_OF, category="fed_decision")

    assert [h.passage.doc_id for h in by_similarity] == ["old-cut-story", "new-cut-story"]
    assert [h.passage.doc_id for h in by_recency] == ["new-cut-story", "old-cut-story"]
    old_hit = by_recency[1]
    assert old_hit.recency == pytest.approx(0.5 ** (200 / 30))
    assert old_hit.score == pytest.approx(old_hit.similarity * old_hit.recency)


def test_similarity_floor_drops_weak_passages() -> None:
    query = ["Federal Reserve interest rate decision"]
    with make(config(similarity_floor=-1.0, ranking="similarity")) as k:
        k.index_documents(corpus())
        everything = k.search_evidence(query, as_of=AS_OF, category="fed_decision", k=50)
    sims = sorted(h.similarity for h in everything)
    assert len(sims) == len(corpus())
    floor = (sims[1] + sims[2]) / 2

    with make(config(similarity_floor=floor, ranking="similarity")) as k:
        k.index_documents(corpus())
        kept = k.search_evidence(query, as_of=AS_OF, category="fed_decision", k=50)
    assert [h.passage.passage_id for h in kept] == [
        h.passage.passage_id for h in everything if h.similarity >= floor
    ]
    assert len(kept) == len(corpus()) - 2


def test_unrelated_text_ranks_below_related_text(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents(corpus())
    hits = knowledge.search_evidence(
        ["federal funds rate target range"], as_of=AS_OF, category="fed_decision", k=50
    )
    assert hits[0].passage.doc_id == "fomc-statement-2026-10"
    garden = [h.score for h in hits if h.passage.doc_id == "garden-tomatoes"]
    assert all(score < hits[0].score / 3 for score in garden)


def test_same_query_same_result_and_again_after_reopening(tmp_path: Path) -> None:
    path = tmp_path / "index"
    with make(path=path) as k:
        k.index_documents([*corpus(), *syndicated_copies()])
        first = k.search_evidence(FED_QUERIES, as_of=AS_OF, category="fed_decision")
        assert k.search_evidence(FED_QUERIES, as_of=AS_OF, category="fed_decision") == first
        k.index_documents([*corpus(), *syndicated_copies()])  # idempotent
        assert k.search_evidence(FED_QUERIES, as_of=AS_OF, category="fed_decision") == first

    with make(path=path) as k:
        again = k.search_evidence(FED_QUERIES, as_of=AS_OF, category="fed_decision")
    assert [h.passage for h in again] == [h.passage for h in first]
    assert [h.score for h in again] == pytest.approx([h.score for h in first], abs=1e-6)


def test_embedder_mismatch_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "index"
    with make(path=path) as k:
        k.index_documents(corpus())
    with pytest.raises(EmbedderMismatch, match="built by"):
        ChromaKnowledge(config(), path=path, embedder=HashingEmbedder(512))


def test_reindexing_a_shorter_version_drops_stale_passages(knowledge: ChromaKnowledge) -> None:
    long_doc = doc("changing", "\n\n".join([FED_STATEMENT, FED_SPEECH, CPI_RELEASE]))
    knowledge.index_documents([long_doc])
    assert knowledge.get_passages(["changing#1"], as_of=AS_OF)
    knowledge.index_documents([long_doc.model_copy(update={"text": FED_SPEECH})])
    assert knowledge.get_passages(["changing#0", "changing#1"], as_of=AS_OF)[0].index == 0
    assert knowledge.get_passages(["changing#1"], as_of=AS_OF) == []


def test_get_passages_keeps_order_and_leaves_out_unknown_ids(knowledge: ChromaKnowledge) -> None:
    knowledge.index_documents(corpus())
    ids = ["cpi-2026-10#0", "nope#0", "fomc-statement-2026-10#0"]
    got = knowledge.get_passages(ids, as_of=AS_OF)
    assert [p.passage_id for p in got] == ["cpi-2026-10#0", "fomc-statement-2026-10#0"]
    early = knowledge.get_passages(ids, as_of=datetime(2026, 11, 1, tzinfo=UTC))
    assert [p.passage_id for p in early] == ["fomc-statement-2026-10#0"]


# -- events


def market(ticker: str, event_ticker: str, opens: datetime, closes: datetime) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=event_ticker,
        title=f"{ticker} title",
        subtitle=ticker.rsplit("-", 1)[-1],
        status=MarketStatus.OPEN,
        open_time=opens,
        close_time=closes,
        rules_primary="If the Federal Reserve changes the upper bound of the target range, "
        "the market resolves Yes.",
    )


def event(ticker: str, title: str, opens: datetime, closes: datetime) -> Event:
    return Event(
        event_ticker=ticker,
        series_ticker=ticker.split("-")[0],
        title=title,
        category="fed_decision",
        markets=[
            market(f"{ticker}-{name}", ticker, opens, closes) for name in ("H25", "H0", "C25")
        ],
    )


def test_only_events_open_at_as_of_are_returned(knowledge: ChromaKnowledge) -> None:
    open_now = event(
        "KXFEDDECISION-26DEC",
        "Fed decision in December 2026",
        datetime(2026, 10, 29, tzinfo=UTC),
        datetime(2026, 12, 9, 19, tzinfo=UTC),
    )
    closed = event(
        "KXFEDDECISION-26OCT",
        "Fed decision in October 2026",
        datetime(2026, 9, 17, tzinfo=UTC),
        datetime(2026, 10, 28, 18, tzinfo=UTC),
    )
    not_yet = event(
        "KXFEDDECISION-27JAN",
        "Fed decision in January 2027",
        AS_OF + timedelta(seconds=1),
        datetime(2027, 1, 27, 19, tzinfo=UTC),
    )
    closes_now = event(
        "KXFEDDECISION-26NOV",
        "Fed decision in November 2026",
        datetime(2026, 10, 1, tzinfo=UTC),
        AS_OF,
    )
    assert knowledge.index_events([open_now, closed, not_yet, closes_now]) == 4
    for query in ["Fed decision in October 2026", "Fed decision in January 2027", "Fed"]:
        hits = knowledge.search_events(query, as_of=AS_OF)
        assert [h.event_ticker for h in hits] == ["KXFEDDECISION-26DEC"]
    later = knowledge.search_events("Fed decision", as_of=AS_OF + timedelta(days=1))
    assert {h.event_ticker for h in later} == {"KXFEDDECISION-26DEC", "KXFEDDECISION-27JAN"}


def test_the_event_document_lists_only_strikes_open_at_as_of(
    knowledge: ChromaKnowledge,
) -> None:
    """A ladder strike added later is not in the document matched before it opened."""
    ticker = "KXFEDDECISION-26DEC"
    first = datetime(2026, 10, 29, tzinfo=UTC)
    closes = datetime(2026, 12, 9, 19, tzinfo=UTC)
    added = AS_OF + timedelta(hours=1)
    ladder = event(ticker, "Fed decision in December 2026", first, closes)
    ladder = ladder.model_copy(
        update={
            "markets": [
                *ladder.markets,
                market(f"{ticker}-C50", ticker, added, closes),
                market(f"{ticker}-C75", ticker, added + timedelta(days=1), closes),
            ]
        }
    )
    assert knowledge.index_events([ladder]) == 1
    assert knowledge.index_events([ladder]) == 1  # re-indexing changes nothing

    def document(as_of: datetime) -> str:
        vector = knowledge.index.embed(["Fed decision C50"])[0]
        hits = knowledge.index.query_events(vector, as_of=as_of, n=10)
        assert [h.id.split("@")[0] for h in hits] == [ticker]  # one document at a time
        return hits[0].document

    for before in (first, AS_OF, added - timedelta(microseconds=500_000)):
        text = document(before)
        assert "H0" in text and "C25" in text and "C50" not in text and "C75" not in text
    for after in (added, added + timedelta(hours=1)):
        text = document(after)
        assert "C50" in text and "C75" not in text
    assert "C75" in document(closes - timedelta(seconds=1))

    for as_of in (AS_OF, added, closes - timedelta(seconds=1)):
        hits = knowledge.search_events("Fed decision", as_of=as_of)
        assert [h.event_ticker for h in hits] == [ticker]
    assert knowledge.search_events("Fed decision", as_of=closes) == []


# -- ledger


def entry(
    entry_id: str, question: str, category: str, resolved_at: datetime | None, outcome: bool = True
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        episode_id=f"ep-{entry_id}",
        mode=RunMode.BACKTEST,
        event_ticker="EVT",
        market_ticker=f"MKT-{entry_id}",
        category=category,
        question=question,
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
        probability=0.7,
        market_price=float(Decimal("0.62")),
        outcome=outcome if resolved_at is not None else None,
        resolved_at=resolved_at,
    )


def test_ledger_returns_resolved_before_as_of_same_category_first(
    knowledge: ChromaKnowledge,
) -> None:
    entries = [
        entry(
            "cpi-sep",
            "Will CPI inflation be above 3.0% in September?",
            "cpi",
            datetime(2026, 10, 15, tzinfo=UTC),
        ),
        entry(
            "fed-sep",
            "Will the Fed hold rates in September?",
            "fed_decision",
            datetime(2026, 9, 17, tzinfo=UTC),
        ),
        entry(
            "fed-oct", "Will the Fed cut rates in October?", "fed_decision", AS_OF, outcome=False
        ),
        entry(
            "fed-dec",
            "Will the Fed cut rates in December?",
            "fed_decision",
            AS_OF + timedelta(days=20),
        ),
        entry("open", "Will the Fed cut rates in January?", "fed_decision", None),
    ]
    assert knowledge.index_ledger(entries) == 4
    hits = knowledge.search_ledger(
        "Will CPI inflation be above 3.0%?", as_of=AS_OF, category="fed_decision"
    )
    assert [(h.question, h.category) for h in hits] == [
        ("Will the Fed hold rates in September?", "fed_decision"),
        ("Will CPI inflation be above 3.0% in September?", "cpi"),
    ]
    assert hits[0].probability == 0.7 and hits[0].market_price == 0.62 and hits[0].outcome
    [top] = knowledge.search_ledger("Fed", as_of=AS_OF, category="fed_decision", k=1)
    assert top.category == "fed_decision"


def test_tied_ledger_entries_come_back_in_one_fixed_order() -> None:
    """One market forecast at three horizons gives three rows with the same text.

    Such rows tie on similarity, and a nearest-neighbour index returns ties in an
    order that depends on how the index was built. The search ranks every
    eligible row itself: by similarity, then the later resolution, then the id.
    """
    import itertools

    held = "Will the Fed hold rates in September?"
    earlier = datetime(2026, 9, 17, tzinfo=UTC)
    later = datetime(2026, 9, 20, tzinfo=UTC)
    entries = [
        entry("held-3d", held, "fed_decision", earlier),
        entry("held-7d", held, "fed_decision", earlier),
        entry("held-1d", held, "fed_decision", earlier),
        entry("held-late", held, "fed_decision", later),
        entry("cut", "Will the Fed cut rates in September?", "fed_decision", later),
        entry("cpi", "Will CPI inflation be above 3.0% in September?", "fed_decision", later),
        entry("too-late", held, "fed_decision", AS_OF + timedelta(days=1)),
    ]
    seen = set()
    for order in itertools.islice(itertools.permutations(entries), 0, 5040, 420):
        knowledge = ChromaKnowledge(config(), embedder=HashingEmbedder())
        try:
            assert knowledge.index_ledger(list(order)) == len(entries)
            vector = knowledge.index.embed([held])[0]
            hits = knowledge.index.query_ledger(vector, as_of=AS_OF, n=5, category="fed_decision")
        finally:
            knowledge.close()
        seen.add(tuple(h.id for h in hits))
        assert hits[0].similarity == pytest.approx(1.0)
    # The four exact matches first (the later resolution, then by id), then the nearest other.
    assert seen == {("held-late", "held-1d", "held-3d", "held-7d", "cut")}
