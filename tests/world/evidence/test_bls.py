"""BLS releases: the manual import, publish times, and what happens when BLS refuses."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from pmfa.world.evidence import bls
from pmfa.world.evidence.bls import BlsSource, ManualImportError, import_manual
from pmfa.world.evidence.http import FetchError, PoliteFetcher

from .conftest import FakeFetcher

# Made-up text in the layout of a news.release page: one preformatted block, hard-wrapped
# paragraphs, a fixed-width table. It is test data, not an official release.
CPI_HTML = """<html><head><title>CPI</title></head><body>
<div id="nav"><a href="/">Skip to content</a></div>
<pre id="bodytext">
Transmission of material in this release is embargoed until
8:30 a.m. (ET) Tuesday, November 10, 2026

CONSUMER PRICE INDEX - OCTOBER 2026 (SYNTHETIC TEST TEXT)

The Consumer Price Index for All Urban Consumers (CPI-U) rose
0.3 percent in October on a seasonally adjusted basis, which is
a made-up number for this test.

Table A.   Item      Oct      Sep
           All items 0.3      0.2
           Shelter   0.4      0.4
</pre></body></html>
"""

EMPSIT_TXT = """THE EMPLOYMENT SITUATION -- OCTOBER 2026 (SYNTHETIC TEST TEXT)

Total nonfarm payroll employment changed by a made-up amount in
October, and the unemployment rate was a made-up value.
"""


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def manual_dir(tmp_path):
    d = tmp_path / "manual"
    d.mkdir()
    (d / "bls-cpi-2026-10.html").write_text(CPI_HTML)
    (d / "bls-empsit-2026-10.txt").write_text(EMPSIT_TXT)
    (d / "README.txt").write_text("not a release")
    (d / "cpi-2026-09.html").write_text("wrong name: ignored")
    return d


def test_manual_import_turns_saved_files_into_documents(manual_dir):
    docs = import_manual(manual_dir)
    assert [d.doc_id for d in docs] == [
        "bls:employment_situation:2026-10",
        "bls:cpi_release:2026-10",
    ]  # oldest first: Nov 6 then Nov 10
    cpi = docs[1]
    assert cpi.source == "bls.gov" and cpi.kind == "cpi_release"
    assert cpi.title == "Consumer Price Index for October 2026"
    assert cpi.url == "https://www.bls.gov/news.release/archives/cpi_11102026.htm"
    # 8:30 a.m. Eastern on Nov 10, 2026 is standard time (UTC-5).
    assert cpi.published_at == utc(2026, 11, 10, 13, 30)
    # Hard-wrapped lines are joined, the table keeps its lines, page chrome is gone.
    assert "rose 0.3 percent in October on a seasonally adjusted basis" in cpi.text
    assert "All items 0.3      0.2" in cpi.text
    assert "Skip to content" not in cpi.text
    assert len(cpi.content_hash) == 64


def test_text_without_an_embargo_line_takes_the_scheduled_date(manual_dir):
    emp = import_manual(manual_dir)[0]
    assert emp.published_at == utc(2026, 11, 6, 13, 30)  # schedule: Nov 6, 8:30 a.m. EST
    assert emp.url == "https://www.bls.gov/news.release/archives/empsit_11062026.htm"
    assert "made-up amount in October" in emp.text.replace("\n", " ")


def test_publish_time_in_daylight_saving_time(tmp_path):
    (tmp_path / "bls-cpi-2026-09.txt").write_text("CPI for September. Made up.\n")
    (doc,) = import_manual(tmp_path)
    assert doc.published_at == utc(2026, 10, 14, 12, 30)  # Oct 14, 8:30 a.m. EDT


def test_a_file_with_no_date_anywhere_is_an_error_not_a_guess(tmp_path):
    (tmp_path / "bls-cpi-2031-01.txt").write_text("No embargo line and no schedule entry.")
    with pytest.raises(ManualImportError, match=r"bls-cpi-2031-01\.txt"):
        import_manual(tmp_path)


def test_html_is_preferred_over_text_for_the_same_release(tmp_path):
    (tmp_path / "bls-cpi-2026-10.txt").write_text("plain copy")
    (tmp_path / "bls-cpi-2026-10.html").write_text(CPI_HTML)
    (doc,) = import_manual(tmp_path)
    assert "CONSUMER PRICE INDEX" in doc.text


def test_missing_manual_directory_is_empty(tmp_path):
    assert import_manual(tmp_path / "nowhere") == []


def test_refused_site_stops_after_one_request_and_names_what_is_missing(manual_dir):
    fetcher = FakeFetcher({}, refused=("bls.gov",))
    report = BlsSource(fetcher, manual_dir).fetch_documents(utc(2026, 10, 1), utc(2026, 11, 25))
    # The manual files cover CPI and Employment Situation for October; September's are missing.
    assert [d.doc_id for d in report.documents] == [
        "bls:employment_situation:2026-10",
        "bls:cpi_release:2026-10",
    ]
    assert len(fetcher.calls) == 1  # one refusal, then nothing more is requested
    (line,) = report.failures
    assert "cpi-2026-09" in line and "empsit-2026-09" in line and "cpi-2026-10" not in line


def test_refusal_through_the_real_fetcher_costs_one_request(tmp_path):
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(403, text="Access Denied")

    fetcher = PoliteFetcher(
        tmp_path / "cache",
        user_agent="pmfa-research-client/0.1",
        min_seconds_between_requests=2.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    report = BlsSource(fetcher, tmp_path / "manual").fetch_documents(
        utc(2026, 10, 1), utc(2026, 11, 25)
    )
    assert report.documents == [] and len(sent) == 1
    # A later run reads the remembered refusal and sends nothing.
    again = BlsSource(fetcher, tmp_path / "manual").fetch_documents(
        utc(2026, 10, 1), utc(2026, 11, 25)
    )
    assert again.documents == [] and len(sent) == 1


def test_automatic_fetch_when_the_site_answers(tmp_path):
    url = bls.release_url("cpi_release", utc(2026, 11, 10).date())
    fetcher = FakeFetcher({url: CPI_HTML})
    source = BlsSource(fetcher, tmp_path / "manual")
    doc = source.fetch_release("cpi_release", "2026-10")
    assert doc.doc_id == "bls:cpi_release:2026-10" and doc.published_at == utc(2026, 11, 10, 13, 30)


def test_newest_release_page_holding_a_later_release_is_not_taken(tmp_path):
    latest = bls.release_url("cpi_release")
    fetcher = FakeFetcher({latest: CPI_HTML})  # says November 10; we ask for September
    with pytest.raises(FetchError, match=r"404"):
        BlsSource(fetcher, tmp_path / "manual").fetch_release("cpi_release", "2026-09")


def test_embargo_line_parsing():
    text = "embargoed until 8:30 a.m. (ET) Friday, December 4, 2026"
    assert bls.embargo_time(text) == utc(2026, 12, 4, 13, 30)
    assert bls.embargo_time("nothing here") is None
