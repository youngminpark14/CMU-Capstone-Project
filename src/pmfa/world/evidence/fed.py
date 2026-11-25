"""Federal Reserve evidence: FOMC statements, FOMC minutes and speeches.

Reuse terms (https://www.federalreserve.gov/disclaimer.htm, read 2026-11-25):
"Unless otherwise indicated, information on Board's website is in the public
domain" and "may be copied and distributed without permission." The same page
asks: "Please cite to the Board as the source of the information." Logos and
seals may not be reproduced without written permission, and material marked as
belonging to a non-Board source needs that source's permission. This project
credits the Board of Governors of the Federal Reserve System and commits only
short excerpts of Board pages as test fixtures.

Where the documents are listed
------------------------------
The pages behind https://www.federalreserve.gov/newsevents/pressreleases.htm and
https://www.federalreserve.gov/newsevents/speeches.htm load their rows from two
JSON files, which are the listing sources used here:

- ``/json/ne-press.json``: every press release, newest first, with ``d`` (the
  date and time), ``t`` (title), ``pt`` (press-release type) and ``l`` (path);
- ``/json/ne-speeches.json``: every speech, with ``d``, ``t``, ``s`` (speaker),
  ``lo`` (venue) and ``l`` (path). Rows with a ``stub`` and no ``l`` are repeat
  deliveries of an earlier text and are skipped.

Both files start with a UTF-8 byte-order mark and end with an ``updateDate``
object. The RSS feeds (``/feeds/press_monetary.xml``, ``/feeds/speeches.xml``)
carry the same times but only the 15 newest items, so they add nothing the JSON
does not and are not fetched. Testimony is not collected (the spec names
statements, minutes and speeches).

Publication time
----------------
``published_at`` is the listing's ``d``, read as America/New_York wall-clock
time (checked against the RSS ``pubDate`` of the same items: identical for all
30 newest press releases and speeches on 2026-11-24). Only rows from before
about 2015 have a date without a time. For those the page's own
"For release at 2:00 p.m. EDT" line applies if it has one, then the documented
release time of the kind (2:00 p.m. Eastern for statements and minutes), and
for a speech with no time anywhere, the last second of its day, so a document is
never read earlier than it was out.

What a Document holds
---------------------
``doc_id`` is ``fed:`` plus the page's file name (``fed:monetary20261028a``,
``fed:fomcminutes20261028``, ``fed:waller20261110a``). ``text`` starts with the
title (and, for a speech, the speaker and the venue), followed by the body
paragraphs separated by blank lines. Footnotes, share links, figures and the
press office's contact line are left out. Minutes are a listing row that points
at a short press release; the Document is the full minutes page at
``/monetarypolicy/fomcminutesYYYYMMDD.htm`` (the date is the meeting's last day).
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from bs4 import BeautifulSoup, NavigableString, Tag

from pmfa.records.evidence import Document
from pmfa.world.evidence.common import (
    EASTERN,
    FED_SOURCE,
    STATEMENT_RELEASE,
    FetchReport,
    eastern_to_utc,
    end_of_day_utc,
    make_document,
    to_utc,
)
from pmfa.world.evidence.http import Fetcher, FetchError

BASE = "https://www.federalreserve.gov"
PRESS_LISTING_URL = f"{BASE}/json/ne-press.json"
SPEECH_LISTING_URL = f"{BASE}/json/ne-speeches.json"

KIND_STATEMENT = "fomc_statement"
KIND_MINUTES = "fomc_minutes"
KIND_SPEECH = "speech"

_MINUTES_TITLE = re.compile(
    r"^Minutes of the Federal Open Market Committee,\s*(\w+)\s+(\d+)\s*[-\u2013]\s*(?:(\w+)\s+)?(\d+),\s*(\d{4})"
)
_STATEMENT_TITLE = re.compile(r"\bFOMC statement\s*$", re.IGNORECASE)
_RELEASE_AT = re.compile(r"For release at (\d{1,2}):(\d{2}) ([ap])\.m\.", re.IGNORECASE)


@dataclass(frozen=True)
class FedItem:
    """One row of a listing: enough to fetch the page and to date it."""

    kind: str
    title: str
    url: str
    published_at: datetime
    """UTC. See the module docstring for which time applies."""
    time_known: bool
    """False when the listing gave a date only (the time is then a documented default)."""
    speaker: str = ""
    location: str = ""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_listing_time(d: str, *, kind: str) -> tuple[datetime, bool]:
    """``'10/28/2026 2:00:00 PM'`` (Eastern) to UTC. A date alone gets the default for ``kind``."""
    d = d.strip()
    if " " in d:
        naive = datetime.strptime(d, "%m/%d/%Y %I:%M:%S %p")
        return naive.replace(tzinfo=EASTERN).astimezone(UTC), True
    day = datetime.strptime(d, "%m/%d/%Y").date()
    return _default_time(day, kind), False


def _default_time(day: date, kind: str) -> datetime:
    if kind in (KIND_STATEMENT, KIND_MINUTES):
        return eastern_to_utc(day, STATEMENT_RELEASE)
    return end_of_day_utc(day)


def _json_rows(text: str) -> list[dict]:
    rows = json.loads(text.lstrip("\ufeff"))
    return [r for r in rows if isinstance(r, dict) and r.get("d") and r.get("t")]


def parse_press_listing(text: str) -> list[FedItem]:
    """FOMC statements and minutes from ``ne-press.json``, oldest first."""
    items: list[FedItem] = []
    for row in _json_rows(text):
        if row.get("pt") != "Monetary Policy" or not row.get("l"):
            continue
        title = _clean(row["t"])
        minutes = _MINUTES_TITLE.match(title)
        if minutes:
            kind = KIND_MINUTES
            first_month, _first_day, last_month, last_day, year = minutes.groups()
            meeting_end = datetime.strptime(
                f"{last_month or first_month} {last_day} {year}", "%B %d %Y"
            ).date()
            # The press release is a pointer; the minutes text is on the meeting's own page.
            url = f"{BASE}/monetarypolicy/fomcminutes{meeting_end:%Y%m%d}.htm"
        elif _STATEMENT_TITLE.search(title):
            kind = KIND_STATEMENT
            url = BASE + row["l"]
        else:
            continue
        published_at, known = parse_listing_time(row["d"], kind=kind)
        items.append(FedItem(kind, title, url, published_at, known))
    return sorted(items, key=lambda i: (i.published_at, i.url))


def parse_speech_listing(text: str) -> list[FedItem]:
    """Speeches from ``ne-speeches.json``, oldest first. Repeat rows (no page of their own) are skipped."""
    items: list[FedItem] = []
    for row in _json_rows(text):
        if not row.get("l"):
            continue
        published_at, known = parse_listing_time(row["d"], kind=KIND_SPEECH)
        items.append(
            FedItem(
                KIND_SPEECH,
                _clean(row["t"]),
                BASE + row["l"],
                published_at,
                known,
                speaker=_clean(row.get("s", "")),
                location=_clean(row.get("lo", "")),
            )
        )
    return sorted(items, key=lambda i: (i.published_at, i.url))


# -- pages ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FedPage:
    """What the parser pulls out of one federalreserve.gov page."""

    title: str
    date_text: str | None
    release_time_text: str | None
    speaker: str
    location: str
    paragraphs: list[str]


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return _clean(node.get_text(" ", strip=False)).replace(" ,", ",").replace(" .", ".")


def _block_text(node: Tag) -> str:
    """Paragraph text with ``<br>`` kept as a line break and whitespace collapsed."""
    for br in node.find_all("br"):
        br.replace_with("\n")
    lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", ln).strip() for ln in node.get_text().split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _body_paragraphs(container: Tag) -> list[str]:
    out: list[str] = []
    in_footnotes = False

    def walk(parent: Tag) -> None:
        nonlocal in_footnotes
        for node in parent.children:
            if isinstance(node, NavigableString):
                continue
            if not isinstance(node, Tag) or in_footnotes:
                continue
            classes = node.get("class") or []
            if node.name == "hr":
                in_footnotes = True  # everything after the rule is footnotes
            elif node.name == "p":
                if node.select_one("span.__cf_email__"):
                    continue  # the press office's contact line
                first = next(
                    (
                        c
                        for c in node.children
                        if not (isinstance(c, NavigableString) and not c.strip())
                    ),
                    None,
                )
                lead = (
                    _clean(first.get_text())
                    if isinstance(first, Tag) and first.name == "strong"
                    else ""
                )
                text = _block_text(node)
                if not text or text.startswith("Implementation Note issued"):
                    continue
                if lead and text.startswith(lead + "\n"):
                    out.extend([lead, text[len(lead) + 1 :]])
                else:
                    out.append(text)
            elif node.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                if text := _text(node):
                    out.append(text)
            elif node.name in ("ul", "ol"):
                out.extend(f"- {t}" for li in node.find_all("li") if (t := _text(li)))
            elif node.name == "table":
                rows = [
                    " | ".join(_text(c) for c in tr.find_all(["th", "td"]))
                    for tr in node.find_all("tr")
                ]
                if rows := [r for r in rows if r.strip(" |")]:
                    out.append("\n".join(rows))
            elif node.name == "blockquote":
                if text := _text(node):
                    out.append(text)
            elif (
                node.name == "div"
                and "panel-attachments" not in classes
                and "heading" not in classes
            ):
                walk(node)

    walk(container)
    return out


def parse_fed_page(markup: str) -> FedPage:
    """Parse a press release, speech or minutes page into title, header facts and paragraphs.

    Three templates are handled (all seen on 2026-11-24): a press release or a
    speech has ``div#article`` with a ``div.heading`` (date, title, release time
    or speaker and venue) followed by the body column; minutes have the title and
    body directly in ``div#article``.
    """
    soup = BeautifulSoup(markup, "lxml")
    article = soup.select_one("#article")
    if article is None:
        raise ValueError("no #article container: not a known federalreserve.gov page")
    for marker in article.select('a[href^="#fn"], a[href^="#f"]'):
        marker.decompose()  # footnote markers and "Return to text" links
    for sup in article.find_all("sup"):
        if sup.get_text(strip=True).isdigit():
            sup.decompose()

    heading = article.select_one("div.heading")
    if heading is not None:
        title = _text(heading.select_one("h3.title"))
        date_text = _text(heading.select_one("p.article__time")) or None
        release = _text(heading.select_one("p.releaseTime")) or None
        speaker = _text(heading.select_one("p.speaker"))
        location = _text(heading.select_one("p.location"))
        bodies = [
            d
            for d in article.select("div.col-xs-12.col-sm-8.col-md-8")
            if "heading" not in (d.get("class") or [])
            and "panel-attachments" not in (d.get("class") or [])
        ]
        paragraphs: list[str] = []
        for body in bodies:
            paragraphs.extend(_body_paragraphs(body))
    else:
        h3 = article.find("h3")
        title = _text(h3)
        date_text = release = None
        speaker = location = ""
        paragraphs = _body_paragraphs(article)
        if h3 is not None and paragraphs and paragraphs[0] == title:
            paragraphs = paragraphs[1:]
    return FedPage(title, date_text, release, speaker, location, paragraphs)


def page_release_time(page: FedPage) -> datetime | None:
    """The time printed on the page ("For release at 2:00 p.m. EDT" on the date shown), or None."""
    if not (page.date_text and page.release_time_text):
        return None
    m = _RELEASE_AT.search(page.release_time_text)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    day = datetime.strptime(page.date_text, "%B %d, %Y").date()
    return eastern_to_utc(day, time(hour, int(m.group(2))))


def doc_id_for(url: str) -> str:
    """``fed:`` plus the page's file name without its extension."""
    return "fed:" + url.rsplit("/", 1)[-1].removesuffix(".htm")


def build_document(item: FedItem, page: FedPage) -> Document:
    """A Document from a listing row and its parsed page.

    The publish time is the listing's when it had a time; otherwise the page's
    printed release time; otherwise the kind's documented default (already in
    ``item.published_at``).
    """
    published_at = item.published_at
    if not item.time_known and (printed := page_release_time(page)) is not None:
        published_at = printed
    head = [item.title if item.kind != KIND_SPEECH else page.title or item.title]
    if item.kind == KIND_SPEECH:
        speaker = page.speaker or item.speaker
        location = page.location or item.location
        head += [line for line in (speaker, location) if line]
    text = "\n\n".join(["\n".join(head), *page.paragraphs]) + "\n"
    title = (
        f"{item.speaker}: {item.title}" if item.kind == KIND_SPEECH and item.speaker else item.title
    )
    return make_document(
        doc_id=doc_id_for(item.url),
        source=FED_SOURCE,
        kind=item.kind,
        url=item.url,
        title=title,
        published_at=to_utc(published_at),
        text=text,
    )


class FedSource:
    """Lists and fetches Federal Reserve documents through a polite fetcher."""

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def list_items(self, start: datetime, end: datetime, *, refresh: bool = False) -> list[FedItem]:
        """Statements, minutes and speeches with ``start <= published_at <= end``, oldest first.

        ``refresh=True`` re-reads both listings from the site (the live poller);
        otherwise a listing fetched before is read from the cache.
        """
        start, end = to_utc(start), to_utc(end)
        items = parse_press_listing(self._fetcher.get(PRESS_LISTING_URL, refresh=refresh).text)
        items += parse_speech_listing(self._fetcher.get(SPEECH_LISTING_URL, refresh=refresh).text)
        window = [i for i in items if start <= i.published_at <= end]
        return sorted(window, key=lambda i: (i.published_at, i.url))

    def fetch_document(self, item: FedItem) -> Document:
        """Fetch and parse one page. Raises :class:`FetchError` if the page is not there."""
        page = parse_fed_page(self._fetcher.get(item.url).text)
        return build_document(item, page)

    def fetch_documents(
        self,
        start: datetime,
        end: datetime,
        *,
        refresh: bool = False,
        skip: frozenset[str] = frozenset(),
    ) -> FetchReport:
        """Every document in the window. A page that fails is reported, not fatal.

        ``skip`` holds doc ids already in a store; their pages are not fetched.
        """
        report = FetchReport()
        for item in self.list_items(start, end, refresh=refresh):
            if doc_id_for(item.url) in skip:
                continue
            try:
                report.documents.append(self.fetch_document(item))
            except (FetchError, ValueError) as exc:
                report.failures.append(f"{item.url}: {exc}")
        return report
