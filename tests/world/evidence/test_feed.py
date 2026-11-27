"""The generic feed source: RSS and Atom items as secondary documents, polled by the live evidence.

The two feeds are synthetic fixtures written for these tests (see the comment at
the top of each file); no real feed is fetched.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pmfa.config import Config, PathsConfig, SourcesConfig
from pmfa.errors import NotFound
from pmfa.live.triggers import document_triggers
from pmfa.world.clocks import SimClock
from pmfa.world.evidence import feed
from pmfa.world.evidence.corpus import CorpusStore
from pmfa.world.evidence.feed import FeedError, FeedSource
from pmfa.world.evidence.live import LiveEvidence

from .conftest import FakeFetcher, fixture_text

RSS_URL = "https://www.example.org/feeds/notes.xml"
ATOM_URL = "https://blog.example.net/atom.xml"
NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def feeds() -> dict[str, str]:
    return {
        RSS_URL: fixture_text("synthetic_feed.rss.xml"),
        ATOM_URL: fixture_text("synthetic_feed.atom.xml"),
    }


def test_rss_items_become_secondary_documents_credited_to_the_feed_host():
    report = FeedSource(FakeFetcher(feeds())).fetch_documents(RSS_URL, utc(2026, 11, 1), NOW)
    hold, preview = report.documents
    assert hold.source == "example.org" and hold.kind == feed.FEED_KIND
    assert hold.doc_id.startswith("feed:example.org:")
    assert hold.url == "https://www.example.org/notes/hold-in-december"
    assert hold.published_at == utc(2026, 11, 24, 20, 30)  # -0500 read as UTC
    assert hold.text == (
        "Analysts expect a hold in December\n\n"
        "Most analysts surveyed expect the committee to hold rates.\n\n"
        "A minority expect a cut.\n"
    )
    # Full content is preferred over the summary.
    assert "Forecasters see 2.9 percent." in preview.text and "short summary" not in preview.text
    assert preview.published_at == utc(2026, 11, 25, 9)
    # The undated item is skipped and reported; the old one is outside the window.
    assert report.failures == [f"feed {RSS_URL}: 1 item(s) without a publication time were skipped"]
    # Ids are stable: the same feed read again gives the same documents.
    again = FeedSource(FakeFetcher(feeds())).fetch_documents(RSS_URL, utc(2026, 11, 1), NOW)
    assert again.documents == report.documents


def test_atom_entries_use_the_alternate_link_and_skip_a_time_without_a_zone():
    report = FeedSource(FakeFetcher(feeds())).fetch_documents(ATOM_URL, utc(2026, 11, 1), NOW)
    [entry] = report.documents
    assert entry.source == "blog.example.net"
    assert entry.url == "https://blog.example.net/reading-the-minutes"
    assert entry.published_at == utc(2026, 11, 25, 10)  # published wins over updated
    assert entry.text == "Reading the minutes\n\nParticipants saw inflation as somewhat elevated.\n"
    assert "1 item(s) without a publication time" in report.failures[0]


def test_a_feed_that_fails_or_is_not_a_feed_is_reported_not_raised():
    fetcher = FakeFetcher({RSS_URL: "<html><body>not a feed</body></html>"})
    source = FeedSource(fetcher)
    assert (
        "not an RSS or Atom feed"
        in source.fetch_documents(RSS_URL, utc(2026, 1, 1), NOW).failures[0]
    )
    missing = source.fetch_documents(ATOM_URL, utc(2026, 1, 1), NOW)
    assert missing.documents == [] and "404" in missing.failures[0]
    with pytest.raises(FeedError, match="entities"):
        feed.parse_feed('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>')
    with pytest.raises(FeedError, match="http"):
        source.items("file:///etc/hosts")


def live(tmp_path, fetcher, feed_urls: list[str]) -> LiveEvidence:
    config = Config(
        paths=PathsConfig(data_dir=str(tmp_path / "data")),
        sources=SourcesConfig(
            federal_reserve=False, bls=False, manual_dir=str(tmp_path / "manual"), feeds=feed_urls
        ),
    )
    return LiveEvidence(config, fetcher, CorpusStore(tmp_path / "corpus"), clock=SimClock(NOW))


def test_no_feed_is_fetched_by_default():
    assert Config().sources.feeds == []


def test_the_live_evidence_polls_listed_feeds_and_their_items_never_trigger(tmp_path):
    fetcher = FakeFetcher(feeds())
    evidence = live(tmp_path, fetcher, [RSS_URL, ATOM_URL])
    added = evidence.poll(force=True)
    assert {d.source for d in added} == {"example.org", "blog.example.net"}
    assert (RSS_URL, True) in fetcher.calls  # re-read from the site on every poll
    assert len(evidence.documents(as_of=NOW)) == 3
    # Feeds are secondary: their items start no new-document trigger.
    assert document_triggers(added, Config()) == []
    assert evidence.poll(force=True) == []  # nothing new the second time


def test_a_feed_on_a_primary_source_host_is_refused(tmp_path):
    fed_feed = "https://www.federalreserve.gov/feeds/press_monetary.xml"
    fetcher = FakeFetcher({fed_feed: fixture_text("synthetic_feed.rss.xml")})
    evidence = live(tmp_path, fetcher, [fed_feed])
    assert evidence.poll(force=True) == []
    assert fetcher.calls == []  # not even fetched
    assert "primary source" in evidence.last_failures[0]


def test_a_feed_item_rechecks_against_the_feed_as_it_is_now(tmp_path):
    pages = feeds()
    fetcher = FakeFetcher(pages)
    evidence = live(tmp_path, fetcher, [RSS_URL])
    [hold, _] = evidence.poll(force=True)
    assert evidence.recheck(hold.doc_id, as_of=NOW).content_hash == hold.content_hash

    pages[RSS_URL] = pages[RSS_URL].replace("A minority expect a cut.", "Nobody expects a cut.")
    evidence = live(tmp_path, fetcher, [RSS_URL])  # a new process: nothing re-checked yet
    assert evidence.recheck(hold.doc_id, as_of=NOW).content_hash != hold.content_hash

    # An item the feed no longer lists is not a withdrawal: feeds drop old items.
    pages[RSS_URL] = pages[RSS_URL].replace("example-note-0001", "example-note-9999")
    evidence = live(tmp_path, fetcher, [RSS_URL])
    assert evidence.recheck(hold.doc_id, as_of=NOW) == hold


def test_a_feed_on_a_subdomain_of_a_primary_source_host_is_refused(tmp_path):
    # press.federalreserve.gov would otherwise be credited as a source of its own.
    sub_feed = "https://press.federalreserve.gov/feeds/press_monetary.xml"
    lookalike = "https://notfederalreserve.gov/feeds/notes.xml"
    fetcher = FakeFetcher(
        {
            sub_feed: fixture_text("synthetic_feed.rss.xml"),
            lookalike: fixture_text("synthetic_feed.rss.xml"),
        }
    )
    evidence = live(tmp_path, fetcher, [sub_feed, lookalike])
    added = evidence.poll(force=True)
    assert (sub_feed, True) not in fetcher.calls  # not even fetched
    assert any("press.federalreserve.gov is a primary source" in f for f in evidence.last_failures)
    # A host that only ends in the same letters is not a subdomain.
    assert {d.source for d in added} == {"notfederalreserve.gov"}


def test_a_feed_item_rechecks_against_every_listed_feed_on_its_host(tmp_path):
    # Two feeds on one host: the item came from the second, so the first does not list it.
    second = "https://www.example.org/feeds/second.xml"
    # The first is an Atom feed on the same host with other items.
    pages = {RSS_URL: fixture_text("synthetic_feed.atom.xml"), second: feeds()[RSS_URL]}
    evidence = live(tmp_path, FakeFetcher(pages), [second])
    [hold, _] = evidence.poll(force=True)

    pages[second] = pages[second].replace("A minority expect a cut.", "Nobody expects a cut.")
    evidence = live(tmp_path, FakeFetcher(pages), [RSS_URL, second])
    rechecked = evidence.recheck(hold.doc_id, as_of=NOW)
    assert rechecked.content_hash != hold.content_hash  # the second feed's current copy
    assert "Nobody expects a cut." in rechecked.text


SECOND = "https://www.example.org/feeds/second.xml"


def dropped(pages: dict[str, str]) -> dict[str, str]:
    """``pages`` with the hold note gone from the second feed, as feeds drop old items."""
    return {**pages, SECOND: pages[SECOND].replace("example-note-0001", "example-note-9999")}


def test_a_sibling_feeds_failure_does_not_outrank_a_feed_that_dropped_the_item(tmp_path):
    # The item came from SECOND, which has since dropped it; RSS_URL on the same host is 404.
    pages = {SECOND: feeds()[RSS_URL]}
    [hold, _] = live(tmp_path, FakeFetcher(pages), [SECOND]).poll(force=True)
    evidence = live(tmp_path, FakeFetcher(dropped(pages)), [SECOND, RSS_URL])  # a new process
    assert evidence.recheck(hold.doc_id, as_of=NOW) == hold  # the stored copy, not withdrawn
    notes = " | ".join(evidence.last_failures)
    assert f"{SECOND} no longer lists it" in notes
    assert f"{RSS_URL} failed" in notes and "404" in notes


def test_a_feed_item_rechecks_against_the_feed_it_came_from_when_that_is_known(tmp_path):
    pages = {SECOND: feeds()[RSS_URL]}
    fetcher = FakeFetcher(pages)
    evidence = live(tmp_path, fetcher, [RSS_URL, SECOND])  # RSS_URL answers 404
    [hold, _] = evidence.poll(force=True)
    fetcher.calls.clear()
    fetcher.pages = dropped(pages)
    assert evidence.recheck(hold.doc_id, as_of=NOW) == hold
    assert fetcher.calls == [(SECOND, True)]  # only the item's own feed is asked
    assert any(f"{SECOND} no longer lists it" in f for f in evidence.last_failures)


def test_a_feed_item_is_withdrawn_only_when_every_feed_asked_is_gone(tmp_path):
    pages = {SECOND: feeds()[RSS_URL]}
    [hold, _] = live(tmp_path, FakeFetcher(pages), [SECOND]).poll(force=True)
    # One feed gone, one unreachable: the item's own feed cannot be told apart, so keep it.
    flaky = live(tmp_path, FakeFetcher({}, refused=("second.xml",)), [SECOND, RSS_URL])
    assert flaky.recheck(hold.doc_id, as_of=NOW) == hold
    # Every listed feed on the host is gone: the item is withdrawn, and the feed is named.
    gone = live(tmp_path, FakeFetcher({}), [SECOND, RSS_URL])
    with pytest.raises(NotFound) as exc:
        gone.recheck(hold.doc_id, as_of=NOW)
    assert SECOND in str(exc.value) and hold.url not in str(exc.value)


def test_a_feed_on_a_primary_source_host_with_a_trailing_dot_is_refused(tmp_path):
    dotted = "https://federalreserve.gov./feeds/press_monetary.xml"
    fetcher = FakeFetcher({dotted: fixture_text("synthetic_feed.rss.xml")})
    evidence = live(tmp_path, fetcher, [dotted])
    assert evidence.poll(force=True) == []
    assert fetcher.calls == []  # not even fetched
    assert "primary source" in evidence.last_failures[0]
