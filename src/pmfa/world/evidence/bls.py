"""BLS evidence: Consumer Price Index and Employment Situation news releases.

Reuse terms (https://www.bls.gov/bls/linksite.htm, read 2026-11-25): "The Bureau
of Labor Statistics (BLS) is a Federal government agency and everything that we
publish, both in hard copy and electronically, is in the public domain, except
for previously copyrighted photographs and illustrations." "You are free to use
our public domain material without specific permission, although we do ask that
you cite the Bureau of Labor Statistics as the source." The BLS emblem is a
federally registered trademark and may not be used without permission. This
project credits the U.S. Bureau of Labor Statistics and does not use the emblem.

Access: what BLS allows (checked 2026-11-25)
--------------------------------------------
- ``https://www.bls.gov/robots.txt`` itself answers a plain client with HTTP 403
  (Akamai "Access Denied"). As read through a web-page reader, it lets every
  crawler fetch the release pages and only blocks scripts, ``/crs``,
  ``/_private`` and print versions; it has no ``Crawl-delay``.
- The BLS terms of use (https://www.bls.gov/bls/blsterms.htm) say: "excessive
  robot activity on BLS websites is prohibited", "BLS will block robots that
  access the website in any way that BLS considers excessive or malicious", and
  "BLS also reserves the right to block robots that do not contain information
  that can be used to contact the owner." So BLS expects an automated client to
  carry contact details.
- Tested with the generic User-Agent ``pmfa-research-client/0.1`` (six requests
  in all, at least three seconds apart): ``/robots.txt``, ``/bls/linksite.htm``,
  ``/bls/pss.htm``, ``/schedule/news_release/cpi.htm`` and
  ``/schedule/news_release/bls.ics`` all answered 403 from ``AkamaiGHost``. The
  one request to ``api.bls.gov`` (``/publicAPI/v1/timeseries/data/CUUR0000SA0``,
  no key) answered 200; that API serves data series, not release dates or the
  release text, and the v1 endpoint is limited to 25 queries a day.
- Decision: this project does not put a name, an email address or a phone number
  in a User-Agent. A generic one is refused, so the automatic fetch is attempted
  once per release (the refusal is remembered and the rest are not tried), and
  the supported path for BLS text is the manual import below. Release dates come
  from the committed schedule (``calendar.py``), not from the site.

Manual import (the steps docs/evaluation.md quotes)
---------------------------------------------------
1. Open the release in a browser. CPI: https://www.bls.gov/news.release/cpi.nr0.htm
   (the newest) or ``https://www.bls.gov/news.release/archives/cpi_MMDDYYYY.htm``
   for an older one. Employment Situation:
   https://www.bls.gov/news.release/empsit.nr0.htm or
   ``https://www.bls.gov/news.release/archives/empsit_MMDDYYYY.htm``. ``MMDDYYYY``
   is the release date.
2. Save the page (browser "Save Page As", HTML only) or copy the release text
   into a plain text file.
3. Put the file in ``config.sources.manual_dir`` (``data/manual`` by default)
   named ``bls-<series>-<YYYY>-<MM>.<ext>``, where ``<series>`` is ``cpi`` or
   ``empsit``, ``<YYYY>-<MM>`` is the *reference month* the release covers (the
   October 2026 CPI, published in November, is ``bls-cpi-2026-10.html``), and
   ``<ext>`` is ``html``, ``htm`` or ``txt``.
4. Run the corpus build again. :func:`import_manual` turns each file into a
   Document. Files whose names do not follow the scheme are ignored.

The publish time of an imported release is the embargo line printed in the
release ("embargoed until 8:30 a.m. (ET) Tuesday, November 10, 2026"); if the
saved file lacks it, the date in the committed BLS schedule at 8:30 a.m. Eastern.
A file with neither is an error, never a guess.

What a Document holds
---------------------
``doc_id`` is ``bls:cpi_release:2026-10`` or ``bls:employment_situation:2026-10``;
``kind`` is ``cpi_release`` or ``employment_situation``; ``source`` is
``bls.gov``; ``url`` is the release's permanent archive address
(``.../news.release/archives/cpi_11102026.htm``); ``title`` is
"Consumer Price Index for October 2026". The text keeps the release's paragraphs;
tables stay as the lines the release prints.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from pmfa.errors import PmfaError
from pmfa.records.evidence import Document
from pmfa.world.evidence.calendar import BLS_TITLES, committed_bls_schedule
from pmfa.world.evidence.common import (
    BLS_SOURCE,
    EASTERN,
    FetchReport,
    eastern_to_utc,
    make_document,
    to_utc,
)
from pmfa.world.evidence.http import Fetcher, FetchError, FetchRefused

NEWS_RELEASE_BASE = "https://www.bls.gov/news.release"
SERIES_BY_KIND = {"cpi_release": "cpi", "employment_situation": "empsit"}
KIND_BY_SERIES = {v: k for k, v in SERIES_BY_KIND.items()}

_MANUAL_NAME = re.compile(r"^bls[-_](cpi|empsit)[-_](\d{4})-(\d{2})\.(html?|txt)$", re.IGNORECASE)
_EMBARGO = re.compile(
    r"embargoed\s+until\s+(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\s*\(?(?:ET|EST|EDT)\)?,?\s*"
    r"(?:[A-Za-z]+day,?\s+)?([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

Schedule = Mapping[str, Mapping[str, tuple[date, time]]]


class ManualImportError(PmfaError):
    """A manually saved BLS file cannot be turned into a Document."""


def release_url(kind: str, released: date | None = None) -> str:
    """The release's archive address for a release date, or the newest release's address."""
    series = SERIES_BY_KIND[kind]
    if released is None:
        return f"{NEWS_RELEASE_BASE}/{series}.nr0.htm"
    return f"{NEWS_RELEASE_BASE}/archives/{series}_{released:%m%d%Y}.htm"


def doc_id_for(kind: str, period: str) -> str:
    return f"bls:{kind}:{period}"


def title_for(kind: str, period: str) -> str:
    year, month = period.split("-")
    return f"{BLS_TITLES[kind]} for {date(int(year), int(month), 1):%B %Y}"


# -- text ----------------------------------------------------------------------------------


def _unwrap(text: str) -> str:
    """Join the hard-wrapped lines of each paragraph; keep table-like blocks line by line."""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    out = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        tabular = any(re.search(r"\S {3,}\S", ln) for ln in lines)
        out.append("\n".join(lines) if tabular else " ".join(ln.strip() for ln in lines))
    return "\n\n".join(out)


_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "table", "pre", "blockquote")


def _blocks(root: Tag) -> list[str]:
    out: list[str] = []
    for el in root.find_all(_BLOCK_TAGS):
        if el.find_parent(_BLOCK_TAGS):
            continue  # its parent block already holds this text
        if el.name == "table":
            rows = [
                " | ".join(
                    re.sub(r"\s+", " ", c.get_text(" ")).strip() for c in tr.find_all(["th", "td"])
                )
                for tr in el.find_all("tr")
            ]
            if text := "\n".join(r for r in rows if r.strip(" |")):
                out.append(text)
        elif el.name == "pre":
            out.append(el.get_text())
        elif text := re.sub(r"\s+", " ", el.get_text(" ")).strip():
            out.append(text)
    return out


def extract_release_text(content: str) -> str:
    """Clean text with paragraphs from a saved BLS release (HTML or plain text)."""
    if not re.search(r"<\s*(html|body|pre|p|div|table)\b", content, re.IGNORECASE):
        return _unwrap(content)
    soup = BeautifulSoup(content, "lxml")
    for junk in soup(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        junk.decompose()
    pres = [p for p in soup.find_all("pre") if len(p.get_text()) > 200]
    if pres:
        # The release text of news.release pages is one preformatted block.
        return _unwrap("\n\n".join(p.get_text() for p in pres))
    root = soup.select_one("#bodytext, #main-content, main, article") or soup.body or soup
    return _unwrap("\n\n".join(_blocks(root)))


def embargo_time(text: str) -> datetime | None:
    """The release time printed in the text ("embargoed until 8:30 a.m. (ET) ..."), as UTC."""
    m = _EMBARGO.search(text)
    if not m:
        return None
    hour, minute, ampm, month_name, day, year = m.groups()
    try:
        month = _MONTHS.index(month_name.lower()[:3]) + 1
        released = date(int(year), month, int(day))
    except ValueError:
        return None
    return eastern_to_utc(
        released, time(int(hour) % 12 + (12 if ampm.lower() == "p" else 0), int(minute))
    )


def build_document(
    kind: str, period: str, content: str, *, scheduled: tuple[date, time] | None
) -> Document:
    """A Document from a saved or fetched release.

    The publish time is the embargo line in the release when it has one,
    otherwise the committed schedule's date and time. With neither, this raises
    :class:`ManualImportError`.
    """
    text = extract_release_text(content)
    published_at = embargo_time(text)
    if published_at is None and scheduled is not None:
        published_at = eastern_to_utc(scheduled[0], scheduled[1])
    if published_at is None:
        raise ManualImportError(
            f"{kind} {period}: no release time in the text and no date in the BLS schedule"
        )
    released = published_at.astimezone(EASTERN).date()
    return make_document(
        doc_id=doc_id_for(kind, period),
        source=BLS_SOURCE,
        kind=kind,
        url=release_url(kind, released),
        title=title_for(kind, period),
        published_at=published_at,
        text=text + "\n",
    )


def import_manual(manual_dir: str | Path, *, schedule: Schedule | None = None) -> list[Document]:
    """Documents from the release files a person saved into ``manual_dir`` (see the module docstring).

    Oldest first. A file whose name does not follow the scheme is ignored. When
    one release has several files, ``.html`` is preferred over ``.htm`` over ``.txt``.
    """
    directory = Path(manual_dir)
    if not directory.is_dir():
        return []
    schedule = schedule if schedule is not None else committed_bls_schedule()
    order = {"html": 0, "htm": 1, "txt": 2}
    chosen: dict[tuple[str, str], tuple[int, Path]] = {}
    for path in sorted(directory.iterdir()):
        m = _MANUAL_NAME.match(path.name)
        if not m or not path.is_file():
            continue
        series, year, month, ext = m.group(1).lower(), m.group(2), m.group(3), m.group(4).lower()
        key = (KIND_BY_SERIES[series], f"{year}-{month}")
        rank = order[ext]
        if key not in chosen or rank < chosen[key][0]:
            chosen[key] = (rank, path)
    docs = []
    for (kind, period), (_, path) in chosen.items():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ManualImportError(f"cannot read {path.name}: {exc}") from exc
        try:
            docs.append(
                build_document(kind, period, content, scheduled=schedule.get(kind, {}).get(period))
            )
        except ManualImportError as exc:
            raise ManualImportError(f"{path.name}: {exc}") from exc
    return sorted(docs, key=lambda d: (d.published_at, d.doc_id))


class BlsSource:
    """BLS releases: manual files first, then an automatic fetch while BLS lets the client in."""

    def __init__(
        self, fetcher: Fetcher | None, manual_dir: str | Path, *, schedule: Schedule | None = None
    ) -> None:
        self._fetcher = fetcher
        self._manual_dir = Path(manual_dir)
        self._schedule = schedule if schedule is not None else committed_bls_schedule()

    def import_manual(self) -> list[Document]:
        return import_manual(self._manual_dir, schedule=self._schedule)

    def fetch_release(self, kind: str, period: str) -> Document:
        """One release by automatic fetch. Raises :class:`FetchRefused` if BLS refuses the client.

        The archive address for the scheduled date is tried first, then the
        newest-release address; a page is accepted only when its embargo line
        names the scheduled day.
        """
        if self._fetcher is None:
            raise FetchRefused("", None, "no fetcher: BLS text comes from manual files")
        scheduled = self._schedule[kind][period]
        urls = [release_url(kind, scheduled[0]), release_url(kind)]
        last: FetchError | None = None
        for url in urls:
            try:
                content = self._fetcher.get(url).text
            except FetchRefused:
                raise
            except FetchError as exc:
                last = exc
                continue
            text_time = embargo_time(extract_release_text(content))
            if text_time is not None and text_time.astimezone(EASTERN).date() != scheduled[0]:
                continue  # the newest-release page now holds a later release
            return build_document(kind, period, content, scheduled=scheduled)
        raise last or FetchError(urls[0], None, f"{kind} {period}: no page carries this release")

    def fetch_documents(
        self, start: datetime, end: datetime, *, skip: frozenset[str] = frozenset()
    ) -> FetchReport:
        """Releases published in ``start <= published_at <= end``: manual files, then automatic fetches.

        A manual file always wins. On the first refusal the automatic path
        stops (nothing more is requested) and one line in ``failures`` names
        the releases that have no document. ``skip`` holds doc ids already in a
        store; those are neither imported nor fetched.
        """
        start, end = to_utc(start), to_utc(end)
        report = FetchReport()
        have: set[str] = set()
        for doc in self.import_manual():
            if start <= doc.published_at <= end:
                have.add(doc.doc_id)
                if doc.doc_id not in skip:
                    report.documents.append(doc)
        due = sorted(
            (eastern_to_utc(day, at), kind, period)
            for kind, releases in self._schedule.items()
            for period, (day, at) in releases.items()
            if start <= eastern_to_utc(day, at) <= end
            and doc_id_for(kind, period) not in have | skip
        )
        missing: list[str] = []
        refused: str | None = None
        for _, kind, period in due:
            label = f"{SERIES_BY_KIND[kind]}-{period}"
            if refused is not None or self._fetcher is None:
                missing.append(label)
                continue
            try:
                report.documents.append(self.fetch_release(kind, period))
            except FetchRefused as exc:
                refused = str(exc)
                missing.append(label)
            except (FetchError, ManualImportError) as exc:
                report.failures.append(f"bls {label}: {exc}")
        if missing:
            why = refused or "no fetcher"
            report.failures.append(
                f"bls.gov: {len(missing)} release(s) have no text ({why}). Save them by hand as "
                f"bls-<cpi|empsit>-<YYYY>-<MM>.html in the manual directory: " + ", ".join(missing)
            )
        report.documents.sort(key=lambda d: (d.published_at, d.doc_id))
        return report
