"""Federal Reserve pages and listings, from small recorded fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from pmfa.world.evidence import fed
from pmfa.world.evidence.fed import FedItem, FedSource
from pmfa.world.evidence.http import FetchError

from .conftest import FED, FakeFetcher, fixture_text


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_statement_page_parses_to_a_document():
    page = fed.parse_fed_page(fixture_text("fomc_statement_20261028.html"))
    assert page.title == "Federal Reserve issues FOMC statement"
    assert page.date_text == "October 28, 2026"
    assert page.release_time_text.startswith("For release at 2:00 p.m. EDT")
    # Body paragraphs are kept; the press office's contact line and the link line are not.
    assert page.paragraphs[0].startswith("The Federal Open Market Committee approved")
    assert any("maintain the target range" in p for p in page.paragraphs)
    assert not any("media inquiries" in p or "Implementation Note" in p for p in page.paragraphs)
    assert not any("protected" in p for p in page.paragraphs)

    item = FedItem(
        fed.KIND_STATEMENT,
        "Federal Reserve issues FOMC statement",
        f"{FED}/newsevents/pressreleases/monetary20261028a.htm",
        utc(2026, 10, 28, 18),
        True,
    )
    doc = fed.build_document(item, page)
    assert doc.doc_id == "fed:monetary20261028a"
    assert doc.source == "federalreserve.gov" and doc.kind == "fomc_statement"
    assert doc.published_at == utc(2026, 10, 28, 18)
    assert doc.text.startswith("Federal Reserve issues FOMC statement\n\nThe Federal Open")
    assert "\n\n" in doc.text and len(doc.content_hash) == 64


def test_minutes_page_parses_with_headings_and_without_footnotes():
    page = fed.parse_fed_page(fixture_text("fomc_minutes_20261028.html"))
    assert page.title == "Minutes of the Federal Open Market Committee"
    assert page.paragraphs[0] == "October 27\N{EN DASH}28, 2026"  # the meeting dates
    assert "Staff Review and Policymakers' Discussion of Financial Stability" in page.paragraphs
    assert page.paragraphs[1].startswith("A joint meeting of the Federal Open Market Committee")
    assert not any(p.startswith("1 ") or p == "1" for p in page.paragraphs)  # footnote marker gone

    item = fed.parse_press_listing(fixture_text("ne-press.json"))[-1]
    assert item.kind == fed.KIND_MINUTES
    doc = fed.build_document(item, page)
    assert doc.doc_id == "fed:fomcminutes20261028"
    assert doc.title == "Minutes of the Federal Open Market Committee, October 27-28, 2026"
    assert doc.url == f"{FED}/monetarypolicy/fomcminutes20261028.htm"


def test_speech_page_parses_with_speaker_and_venue():
    page = fed.parse_fed_page(fixture_text("speech_20261110.html"))
    assert page.title == "The Economic Outlook and Some Thoughts on Labor Market Dynamism"
    assert page.speaker == "Governor Christopher J. Waller"
    assert page.location.startswith("At the Bank of England Watchers Conference")
    assert len(page.paragraphs) >= 4 and page.paragraphs[0].startswith("Thank you, Paul")

    item = fed.parse_speech_listing(fixture_text("ne-speeches.json"))[0]
    doc = fed.build_document(item, page)
    assert doc.doc_id == "fed:waller20261110a"
    assert doc.kind == "speech"
    assert doc.title.startswith("Governor Christopher J. Waller: The Economic Outlook")
    lines = doc.text.split("\n\n")[0].split("\n")
    assert lines[1] == "Governor Christopher J. Waller" and lines[2].startswith("At the Bank")
    assert doc.published_at == utc(2026, 11, 10, 9)  # 4:00 a.m. EST


def test_listing_times_are_eastern_in_daylight_and_standard_time():
    summer, _ = fed.parse_listing_time("10/28/2026 2:00:00 PM", kind=fed.KIND_STATEMENT)
    winter, _ = fed.parse_listing_time("11/18/2026 2:00:00 PM", kind=fed.KIND_MINUTES)
    assert summer == utc(2026, 10, 28, 18)  # EDT is UTC-4
    assert winter == utc(2026, 11, 18, 19)  # EST is UTC-5
    # The change of clocks: 2026-11-01 is the first standard-time day.
    before, _ = fed.parse_listing_time("10/31/2026 2:00:00 PM", kind=fed.KIND_STATEMENT)
    after, _ = fed.parse_listing_time("11/1/2026 2:00:00 PM", kind=fed.KIND_STATEMENT)
    assert (before.hour, after.hour) == (18, 19)


def test_date_only_rows_use_documented_defaults_and_the_page_time():
    at, known = fed.parse_listing_time("1/3/2006", kind=fed.KIND_STATEMENT)
    assert (at, known) == (utc(2006, 1, 3, 19), False)  # 2:00 p.m. EST
    speech_at, _ = fed.parse_listing_time("7/1/2009", kind=fed.KIND_SPEECH)
    assert speech_at == utc(2009, 7, 2, 3, 59, 59)  # end of the day in Eastern time

    # When a date-only row's page prints a release time, that time is used.
    page = fed.parse_fed_page(fixture_text("fomc_statement_20261028.html"))
    item = FedItem(
        fed.KIND_STATEMENT, "t", f"{FED}/x/monetary20261028a.htm", utc(2026, 10, 29, 3), False
    )
    assert fed.build_document(item, page).published_at == utc(2026, 10, 28, 18)


def test_press_listing_keeps_statements_and_minutes_only():
    items = fed.parse_press_listing(fixture_text("ne-press.json"))
    assert [(i.kind, i.published_at) for i in items] == [
        (fed.KIND_STATEMENT, utc(2006, 1, 3, 19)),
        (fed.KIND_STATEMENT, utc(2026, 9, 16, 18)),
        (fed.KIND_STATEMENT, utc(2026, 10, 28, 18)),
        (fed.KIND_MINUTES, utc(2026, 11, 18, 19)),
    ]  # the discount-rate minutes and the schedule notice are not FOMC documents
    assert items[-1].url.endswith("/monetarypolicy/fomcminutes20261028.htm")


def test_speech_listing_skips_repeat_rows_and_decodes_entities():
    items = fed.parse_speech_listing(fixture_text("ne-speeches.json"))
    assert [i.published_at for i in items] == [utc(2026, 11, 10, 9), utc(2026, 11, 20, 17, 15)]
    assert items[1].title == "The U.S. Economy in Global Context"
    assert items[1].speaker == "Governor Lisa D. Cook"


def test_list_items_window_and_fetch_documents(fed_pages):
    fetcher = FakeFetcher(fed_pages)
    source = FedSource(fetcher)
    items = source.list_items(utc(2026, 10, 1), utc(2026, 11, 10, 9))
    assert [i.url.rsplit("/", 1)[-1] for i in items] == [
        "monetary20261028a.htm",
        "waller20261110a.htm",
    ]
    # The window is closed at both ends: a document published exactly at ``end`` is in.
    exact = source.list_items(utc(2026, 11, 10, 9), utc(2026, 11, 10, 9))
    assert [i.url.rsplit("/", 1)[-1] for i in exact] == ["waller20261110a.htm"]

    report = source.fetch_documents(utc(2026, 9, 1), utc(2026, 11, 30))
    assert [d.doc_id for d in report.documents] == [
        "fed:monetary20261028a",
        "fed:waller20261110a",
        "fed:fomcminutes20261028",
    ]
    # Two pages are not in the recorded set (the September statement and Cook's speech).
    assert len(report.failures) == 2 and all("404" in f for f in report.failures)

    # Documents already in a store are not fetched again.
    fetcher.calls.clear()
    done = frozenset(d.doc_id for d in report.documents)
    source.fetch_documents(utc(2026, 9, 1), utc(2026, 11, 30), skip=done)
    fetched = {url.rsplit("/", 1)[-1] for url, _ in fetcher.calls}
    assert not fetched & {"monetary20261028a.htm", "waller20261110a.htm", "fomcminutes20261028.htm"}


def test_refreshing_listings_is_passed_to_the_fetcher(fed_pages):
    fetcher = FakeFetcher(fed_pages)
    FedSource(fetcher).list_items(utc(2026, 10, 1), utc(2026, 11, 30), refresh=True)
    assert [refresh for _, refresh in fetcher.calls] == [True, True]


def test_a_listing_that_fails_is_reported_and_the_other_listing_still_read(fed_pages):
    # federalreserve.gov's press listing is down (a 503 here); the speech listing still answers.
    class Unavailable(FakeFetcher):
        def get(self, url: str, *, refresh: bool = False):  # type: ignore[no-untyped-def]
            if url.endswith("ne-press.json"):
                raise FetchError(url, 503, f"{url} answered HTTP 503")
            return super().get(url, refresh=refresh)

    report = FedSource(Unavailable(fed_pages)).fetch_documents(
        utc(2026, 9, 1), utc(2026, 11, 30), refresh=True
    )
    assert [d.doc_id for d in report.documents] == ["fed:waller20261110a"]
    assert any("ne-press.json" in f and "503" in f for f in report.failures)
