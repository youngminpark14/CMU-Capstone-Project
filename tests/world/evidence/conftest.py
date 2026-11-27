"""Shared helpers for the evidence tests: recorded pages and a fetcher that never touches the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from pmfa.world.evidence.http import Fetched, FetchError, FetchRefused

FIXTURES = Path(__file__).parent / "fixtures"
FED = "https://www.federalreserve.gov"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeFetcher:
    """A Fetcher over a dict of url -> text. A missing url is a 404; ``refused`` hosts answer 403."""

    def __init__(self, pages: dict[str, str], refused: tuple[str, ...] = ()) -> None:
        self.pages = pages
        self.refused = refused
        self.calls: list[tuple[str, bool]] = []

    def get(self, url: str, *, refresh: bool = False) -> Fetched:
        self.calls.append((url, refresh))
        if any(host in url for host in self.refused):
            raise FetchRefused(url, 403, f"refused: HTTP 403 for {url}")
        if url not in self.pages:
            raise FetchError(url, 404, f"{url} answered HTTP 404")
        return Fetched(url=url, status=200, content=self.pages[url].encode(), from_cache=False)


@pytest.fixture
def fed_pages() -> dict[str, str]:
    """The recorded Federal Reserve pages, by the URL they were fetched from."""
    return {
        f"{FED}/json/ne-press.json": fixture_text("ne-press.json"),
        f"{FED}/json/ne-speeches.json": fixture_text("ne-speeches.json"),
        f"{FED}/newsevents/pressreleases/monetary20261028a.htm": fixture_text(
            "fomc_statement_20261028.html"
        ),
        f"{FED}/monetarypolicy/fomcminutes20261028.htm": fixture_text("fomc_minutes_20261028.html"),
        f"{FED}/newsevents/speech/waller20261110a.htm": fixture_text("speech_20261110.html"),
        f"{FED}/monetarypolicy/fomccalendars.htm": fixture_text("fomc_calendar.html"),
    }
