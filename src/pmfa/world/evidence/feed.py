"""A generic feed source: RSS or Atom items as secondary evidence for the live modes.

Open-web news is out of scope for v1 (the spec's Out of Scope list: news wires,
social media and open-web search). This module exists so the Evidence port can
take such a source later, as build step 13 asks: ``config.sources.feeds`` is an
empty list by default, so nothing here runs unless a user lists a feed URL.

What a listed feed gives
------------------------
:class:`~pmfa.world.evidence.live.LiveEvidence` polls each listed feed with the
same polite fetcher as the official sources, and every item published in the
poll's window becomes a :class:`~pmfa.records.evidence.Document`:

- ``source`` is the feed's host without a leading ``www.``
  (``https://www.example.org/rss.xml`` gives ``example.org``);
- ``doc_id`` is ``feed:<host>:<hash>``, the hash taken from the item's ``guid`` or
  ``id`` (else its link, else its title and date), so it is stable across polls;
- ``kind`` is ``feed_item``; ``url`` is the item's link (the feed's URL without one);
- ``text`` is the title, a blank line, and the item's content (or its summary)
  as plain text, HTML tags removed;
- ``published_at`` is the item's ``pubDate`` (RSS), ``published`` or ``updated``
  (Atom) or ``dc:date``. An item without a time, or with a time that names no
  zone, is skipped and reported: a document is never dated by guess.

Secondary only
--------------
A feed is never a primary source. The poller refuses a feed whose host is a
primary source of any series in ``config.follow`` (that would let a feed
stand in for an official release), so feed items cannot start a new-document
trigger and need a second independent origin under the origin rule. Item text
is untrusted like all evidence text.

Both RSS (0.9x, 1.0 and 2.0) and Atom are read by element name, whatever the
namespace. A feed that declares entities (``<!ENTITY``) is refused rather than
parsed. Feeds are not part of the frozen corpus: backtests do not read them.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from pmfa.records.evidence import Document
from pmfa.world.evidence.common import FetchReport, make_document, to_utc
from pmfa.world.evidence.http import Fetcher, FetchError

FEED_KIND = "feed_item"

_ITEMS = ("item", "entry")
_CONTENT = ("encoded", "content")
_SUMMARY = ("description", "summary")
_DATES = ("pubDate", "published", "updated", "date")


class FeedError(ValueError):
    """A feed could not be read as RSS or Atom."""


@dataclass(frozen=True)
class FeedItem:
    """One item of a feed, before it becomes a Document."""

    key: str
    """The item's ``guid`` / ``id``, else its link, else its title and date: what the id hashes."""
    title: str
    link: str
    published_at: datetime | None
    """UTC, or None when the item gives no time with a zone."""
    body: str
    """The content or summary as plain text, paragraphs separated by blank lines."""


def feed_host(url: str) -> str:
    """The host a feed's documents are credited to: lower case, without a leading ``www.``."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, names: tuple[str, ...]) -> ET.Element | None:
    for name in names:
        for child in element:
            if _local(child.tag) == name:
                return child
    return None


def _text_of(element: ET.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


_BLOCKS = ("p", "div", "li", "blockquote", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6")


def _plain(markup: str) -> str:
    """Text without HTML tags: one paragraph per block element, whitespace collapsed."""
    if not markup:
        return ""
    if "<" in markup:
        soup = BeautifulSoup(markup, "lxml")
        for br in soup.find_all("br"):
            br.replace_with("\n\n")
        for block in soup.find_all(_BLOCKS):
            block.insert_before("\n\n")
            block.insert_after("\n\n")
        markup = soup.get_text(" ")
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", markup)]
    return "\n\n".join(p for p in paragraphs if p)


def _link(item: ET.Element) -> str:
    for child in item:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href is not None:  # Atom
            if child.get("rel", "alternate") == "alternate":
                return href.strip()
            continue
        if text := _text_of(child):  # RSS
            return text
    return ""


def parse_time(value: str) -> datetime | None:
    """An RSS (RFC 822) or Atom (ISO 8601) time as UTC; None without a zone or when unreadable."""
    value = value.strip()
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None and value.endswith("-0000"):
            moment = moment.replace(tzinfo=UTC)  # RFC 2822: a UTC time, local zone unknown
    if moment.tzinfo is None:
        return None
    return to_utc(moment)


def parse_feed(markup: str) -> list[FeedItem]:
    """The items of an RSS or Atom document, in feed order. Raises :class:`FeedError`."""
    if "<!ENTITY" in markup:
        raise FeedError("the feed declares entities; refused")
    try:
        root = ET.fromstring(markup.lstrip("﻿").strip())
    except ET.ParseError as exc:
        raise FeedError(f"not XML: {exc}") from None
    if _local(root.tag) not in ("rss", "RDF", "feed"):
        raise FeedError(f"not an RSS or Atom feed (root element {_local(root.tag)!r})")
    items = []
    for element in root.iter():
        if _local(element.tag) not in _ITEMS:
            continue
        title = re.sub(r"\s+", " ", _text_of(_child(element, ("title",))))
        link = _link(element)
        when = _text_of(_child(element, _DATES))
        key = _text_of(_child(element, ("guid", "id"))) or link or f"{title}\x1f{when}"
        body = _plain(_text_of(_child(element, _CONTENT)) or _text_of(_child(element, _SUMMARY)))
        items.append(FeedItem(key, title, link, parse_time(when), body))
    return items


def doc_id_for(feed_url: str, item: FeedItem) -> str:
    digest = hashlib.sha256(item.key.encode("utf-8")).hexdigest()[:16]
    return f"feed:{feed_host(feed_url)}:{digest}"


def build_document(feed_url: str, item: FeedItem) -> Document:
    """A Document from a dated feed item (see the module docstring for each field)."""
    if item.published_at is None:
        raise FeedError(f"{item.key}: no publication time with a zone")
    title = item.title or item.link or feed_url
    text = "\n\n".join(part for part in (title, item.body) if part) + "\n"
    return make_document(
        doc_id=doc_id_for(feed_url, item),
        source=feed_host(feed_url),
        kind=FEED_KIND,
        url=item.link or feed_url,
        title=title,
        published_at=item.published_at,
        text=text,
    )


class FeedSource:
    """Reads RSS or Atom feeds through a polite fetcher."""

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    def items(self, feed_url: str, *, refresh: bool = False) -> list[FeedItem]:
        """The feed's items. Raises :class:`FetchError` or :class:`FeedError`."""
        if urlsplit(feed_url).scheme not in ("http", "https"):
            raise FeedError(f"{feed_url!r} is not an http(s) URL")
        return parse_feed(self._fetcher.get(feed_url, refresh=refresh).text)

    def fetch_documents(
        self,
        feed_url: str,
        start: datetime,
        end: datetime,
        *,
        refresh: bool = False,
        skip: frozenset[str] = frozenset(),
    ) -> FetchReport:
        """Items published in ``start <= published_at <= end`` as Documents, oldest first.

        A feed that cannot be fetched or read, and each undated item, is one line
        in ``failures``, never an exception. ``skip`` holds doc ids already stored.
        """
        start, end = to_utc(start), to_utc(end)
        report = FetchReport()
        try:
            items = self.items(feed_url, refresh=refresh)
        except (FetchError, FeedError) as exc:
            report.failures.append(f"feed {feed_url}: {exc}")
            return report
        undated = 0
        for item in items:
            if item.published_at is None:
                undated += 1
                continue
            if not (start <= item.published_at <= end):
                continue
            doc = build_document(feed_url, item)
            if doc.doc_id not in skip:
                report.documents.append(doc)
        if undated:
            report.failures.append(
                f"feed {feed_url}: {undated} item(s) without a publication time were skipped"
            )
        report.documents = list({d.doc_id: d for d in report.documents}.values())
        report.documents.sort(key=lambda d: (d.published_at, d.doc_id))
        return report

    def refetch(self, feed_url: str, document: Document) -> Document | None:
        """The item behind ``document`` as the feed serves it now, or None if the feed no longer lists it.

        Feeds drop old items as a matter of course, so an item missing from the
        feed is not a withdrawal. Raises :class:`FetchError` or :class:`FeedError`.
        """
        for item in self.items(feed_url, refresh=True):
            if item.published_at is not None and doc_id_for(feed_url, item) == document.doc_id:
                return build_document(feed_url, item)
        return None


__all__ = [
    "FEED_KIND",
    "FeedError",
    "FeedItem",
    "FeedSource",
    "build_document",
    "doc_id_for",
    "feed_host",
    "parse_feed",
    "parse_time",
]
