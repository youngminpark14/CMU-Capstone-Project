"""The corpus store, the frozen and live Evidence adapters, and the one-command build."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pmfa.config import Config, PathsConfig, SourcesConfig
from pmfa.errors import NotFound
from pmfa.ports import Evidence
from pmfa.world.clocks import SimClock
from pmfa.world.evidence.calendar import ReleaseCalendar, bls_events, committed_bls_schedule
from pmfa.world.evidence.common import make_document
from pmfa.world.evidence.corpus import CorpusManifest, CorpusStore, FrozenEvidence, build_corpus
from pmfa.world.evidence.live import LiveEvidence

from .conftest import FED, FakeFetcher
from .test_bls import CPI_HTML


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def doc(
    doc_id: str, published_at: datetime, source: str = "federalreserve.gov", text: str = "body"
):
    return make_document(
        doc_id=doc_id,
        source=source,
        kind="speech",
        url=f"https://example.gov/{doc_id}",
        title=doc_id,
        published_at=published_at,
        text=text,
    )


def store_with(tmp_path, docs):
    store = CorpusStore(tmp_path / "corpus")
    store.write(docs, manifest=CorpusManifest(built_at=utc(2026, 11, 25)))
    return store


# -- the store ---------------------------------------------------------------------------


def test_store_round_trip_with_manifest(tmp_path):
    docs = [
        doc("b", utc(2026, 11, 2), "bls.gov", "second"),
        doc("a", utc(2026, 11, 1), text="first"),
        doc("c", utc(2026, 11, 3), text="third"),
    ]
    events = bls_events("cpi_release", committed_bls_schedule()["cpi_release"])[:3]
    store = CorpusStore(tmp_path / "corpus")
    manifest = CorpusManifest(
        built_at=utc(2026, 11, 25, 9),
        window_start=utc(2026, 11, 1),
        window_end=utc(2026, 11, 25),
        failures=["https://example.gov/missing: 404"],
    )
    written = store.write(docs, events=events, manifest=manifest)

    reread = CorpusStore(tmp_path / "corpus")  # a new object over the same files
    assert [d.doc_id for d in reread.documents()] == ["a", "b", "c"]  # oldest first
    assert reread.documents() == store.documents()
    assert reread.events() == sorted(events, key=lambda e: e.scheduled_at)
    stored = reread.manifest()
    assert stored == written
    assert stored.counts == {"bls.gov": 1, "federalreserve.gov": 2}
    assert stored.content_hashes == {d.doc_id: d.content_hash for d in docs}
    assert stored.built_at == utc(2026, 11, 25, 9) and stored.failures == manifest.failures
    assert (tmp_path / "corpus" / "documents.jsonl").read_text().count("\n") == 3
    assert reread.verify() == []


def test_verify_finds_a_changed_text(tmp_path):
    store = store_with(tmp_path, [doc("a", utc(2026, 11, 1), text="original")])
    path = store.documents_path
    path.write_text(path.read_text().replace("original", "edited"))
    assert CorpusStore(store.directory).verify() == ["a"]


def test_add_keeps_existing_documents_and_adds_new_ones(tmp_path):
    store = store_with(tmp_path, [doc("a", utc(2026, 11, 1), text="v1")])
    added = store.add([doc("a", utc(2026, 11, 1), text="v2"), doc("b", utc(2026, 11, 2))])
    assert [d.doc_id for d in added] == ["b"]
    assert store.get("a").text == "v1"
    reread = CorpusStore(store.directory)
    assert [d.doc_id for d in reread.documents()] == ["a", "b"]
    assert reread.manifest().counts == {"federalreserve.gov": 2}


# -- the frozen adapter ------------------------------------------------------------------


@pytest.fixture
def frozen(tmp_path):
    docs = [
        doc("d1", utc(2026, 10, 1, 12)),
        doc("d2", utc(2026, 10, 15, 12)),
        doc("d3", utc(2026, 11, 1, 12)),
        doc("d4", utc(2026, 11, 20, 12)),
    ]
    store = store_with(tmp_path, docs)
    events = bls_events("cpi_release", committed_bls_schedule()["cpi_release"])
    return FrozenEvidence(store, ReleaseCalendar(events))


def test_frozen_evidence_is_an_evidence_port(frozen):
    assert isinstance(frozen, Evidence)


def test_documents_never_include_anything_after_as_of(frozen):
    as_of = utc(2026, 11, 1, 12)
    got = frozen.documents(as_of=as_of)
    assert [d.doc_id for d in got] == ["d1", "d2", "d3"]  # d3 is published exactly at as_of
    assert all(d.published_at <= as_of for d in got)
    assert [d.doc_id for d in frozen.documents(as_of=as_of - timedelta(seconds=1))] == ["d1", "d2"]
    assert frozen.documents(as_of=utc(2026, 9, 1)) == []


def test_published_after_is_exclusive_and_as_of_still_wins(frozen):
    got = frozen.documents(as_of=utc(2026, 11, 30), published_after=utc(2026, 10, 15, 12))
    assert [d.doc_id for d in got] == ["d3", "d4"]  # d2 is published exactly then: not "after"
    early = frozen.documents(as_of=utc(2026, 11, 1, 12), published_after=utc(2026, 10, 15, 12))
    assert [d.doc_id for d in early] == ["d3"]
    assert frozen.documents(as_of=utc(2026, 10, 1), published_after=utc(2026, 11, 1)) == []


def test_times_in_other_zones_are_compared_as_instants(frozen):
    from zoneinfo import ZoneInfo

    as_of = datetime(2026, 11, 1, 8, 0, tzinfo=ZoneInfo("America/New_York"))  # 12:00 UTC
    assert [d.doc_id for d in frozen.documents(as_of=as_of)] == ["d1", "d2", "d3"]


def test_naive_times_are_refused(frozen):
    with pytest.raises(ValueError):
        frozen.documents(as_of=datetime(2026, 11, 1))


def test_get_document_raises_not_found_for_missing_or_future(frozen):
    assert frozen.get_document("d3", as_of=utc(2026, 11, 1, 12)).doc_id == "d3"
    with pytest.raises(NotFound):
        frozen.get_document("d4", as_of=utc(2026, 11, 1, 12))
    with pytest.raises(NotFound):
        frozen.get_document("nope", as_of=utc(2026, 12, 1))


def test_scheduled_events_window_bounds(frozen):
    # CPI releases: 2026-10-14 12:30Z (September) and 2026-11-10 13:30Z (October).
    got = frozen.scheduled_events(start=utc(2026, 10, 1), end=utc(2026, 11, 10, 13, 30))
    assert [e.scheduled_event_id for e in got] == ["cpi_release:2026-09", "cpi_release:2026-10"]
    after_first = frozen.scheduled_events(start=utc(2026, 10, 14, 12, 30), end=utc(2026, 12, 1))
    assert [e.scheduled_event_id for e in after_first] == ["cpi_release:2026-10"]
    assert frozen.scheduled_events(start=utc(2026, 11, 10, 13, 30), end=utc(2026, 11, 20)) == []


# -- the build ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    manual = tmp_path / "manual"
    manual.mkdir()
    (manual / "bls-cpi-2026-10.html").write_text(CPI_HTML)
    return Config(
        paths=PathsConfig(data_dir=str(tmp_path / "data")),
        sources=SourcesConfig(manual_dir=str(manual)),
    )


def test_build_corpus_writes_a_store_that_replays_as_of(config, fed_pages):
    fetcher = FakeFetcher(fed_pages, refused=("bls.gov",))
    manifest = build_corpus(
        config, utc(2026, 9, 1), utc(2026, 11, 30), fetcher, now=utc(2026, 11, 25, 12)
    )
    assert manifest.built_at == utc(2026, 11, 25, 12)
    assert manifest.window_start == utc(2026, 9, 1) and manifest.window_end == utc(2026, 11, 30)
    assert manifest.counts == {"bls.gov": 1, "federalreserve.gov": 3}
    assert set(manifest.content_hashes) == {
        "fed:monetary20261028a",
        "fed:waller20261110a",
        "fed:fomcminutes20261028",
        "bls:cpi_release:2026-10",
    }
    # Failures are reported, not hidden: two Fed pages missing, BLS refused for the other releases.
    assert any("monetary20260916a" in f for f in manifest.failures)
    assert any(f.startswith("bls.gov:") for f in manifest.failures)

    store = CorpusStore(config.paths.corpus_dir())
    assert store.manifest() == manifest and store.verify() == []
    evidence = FrozenEvidence(store)
    docs = evidence.documents(as_of=utc(2026, 11, 12))
    assert [d.doc_id for d in docs] == [
        "fed:monetary20261028a",
        "fed:waller20261110a",
        "bls:cpi_release:2026-10",
    ]
    assert max(d.published_at for d in docs) <= utc(2026, 11, 12)
    events = evidence.scheduled_events(start=utc(2026, 11, 30), end=utc(2026, 12, 31))
    assert [e.scheduled_event_id for e in events] == [
        "employment_situation:2026-11",
        "fomc_decision:2026-12-09",
        "cpi_release:2026-11",
    ]


def test_build_corpus_respects_the_source_switches(config, fed_pages):
    off = config.model_copy(update={"sources": config.sources.model_copy(update={"bls": False})})
    fetcher = FakeFetcher(fed_pages)
    manifest = build_corpus(
        off, utc(2026, 10, 1), utc(2026, 11, 30), fetcher, now=utc(2026, 11, 25)
    )
    assert "bls.gov" not in manifest.counts
    assert not any("bls.gov" in url for url, _ in fetcher.calls)


# -- the live adapter --------------------------------------------------------------------


def test_live_evidence_polls_adds_new_documents_and_applies_as_of(config, fed_pages, tmp_path):
    fetcher = FakeFetcher(fed_pages, refused=("bls.gov",))
    clock = SimClock(utc(2026, 11, 25, 12))
    live = LiveEvidence(config, fetcher, CorpusStore(tmp_path / "live"), clock=clock)
    assert isinstance(live, Evidence)

    got = live.documents(as_of=utc(2026, 11, 12))
    assert [d.doc_id for d in got] == [
        "fed:monetary20261028a",
        "fed:waller20261110a",
        "bls:cpi_release:2026-10",
    ]
    assert all(d.published_at <= utc(2026, 11, 12) for d in got)
    # The listing was re-read from the site, not from the cache.
    assert (f"{FED}/json/ne-press.json", True) in fetcher.calls

    # A call soon after does not poll again; a call after the interval does.
    calls = len(fetcher.calls)
    clock.advance(10)
    live.documents(as_of=clock.now())
    assert len(fetcher.calls) == calls
    clock.advance(600)
    live.documents(as_of=clock.now())
    assert len(fetcher.calls) > calls

    with pytest.raises(NotFound):
        live.get_document("fed:fomcminutes20261028", as_of=utc(2026, 11, 12))
    assert live.get_document("fed:fomcminutes20261028", as_of=clock.now()).kind == "fomc_minutes"


def test_live_poll_returns_only_what_is_new(config, fed_pages, tmp_path):
    fetcher = FakeFetcher(fed_pages, refused=("bls.gov",))
    clock = SimClock(utc(2026, 11, 25, 12))
    live = LiveEvidence(config, fetcher, CorpusStore(tmp_path / "live"), clock=clock)
    first = live.poll()
    assert {d.doc_id for d in first} >= {"fed:monetary20261028a", "fed:fomcminutes20261028"}
    clock.advance(3600)
    assert live.poll() == []


def test_live_scheduled_events_come_from_the_calendar(config, fed_pages, tmp_path):
    fetcher = FakeFetcher(fed_pages, refused=("bls.gov",))
    live = LiveEvidence(
        config, fetcher, CorpusStore(tmp_path / "live"), clock=SimClock(utc(2026, 11, 25))
    )
    events = live.scheduled_events(start=utc(2026, 11, 25), end=utc(2026, 12, 10))
    assert [e.scheduled_event_id for e in events] == [
        "employment_situation:2026-11",
        "fomc_decision:2026-12-09",
    ]
