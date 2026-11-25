"""The release calendar: FOMC decision days and BLS CPI and Employment Situation releases.

Each entry is a :class:`~pmfa.records.evidence.ScheduledEvent` with a stable id:

- ``fomc_decision:2026-12-09``: the decision day (the meeting's last day), at the
  statement time, 2:00 p.m. Eastern;
- ``cpi_release:2026-11``: the CPI release for the reference month, 8:30 a.m. Eastern;
- ``employment_situation:2026-11``: the Employment Situation release, 8:30 a.m. Eastern.

Where the dates come from
-------------------------
**FOMC.** The meeting table on https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm,
one panel per year with a row per meeting (``div.fomc-meeting``: a month label
and a day range such as ``27-28``, with ``*`` marking a meeting with a Summary
of Economic Projections). The statement comes out at 2:00 p.m. Eastern on the
last day (the page lists the statement time of every past meeting as 2:00 p.m.).
The Monetary Policy Strategy Meeting row has no decision and is left out. Dates
are tentative until confirmed at the previous meeting, so rebuild the calendar
when the page changes.

**BLS.** Two sources, in this order:

1. the schedule tables at https://www.bls.gov/schedule/news_release/cpi.htm and
   ``.../empsit.htm`` (columns: reference month, release date, release time),
   parsed by :func:`parse_bls_schedule` when the fetcher is allowed in;
2. ``bls_schedule.json`` next to this module: the official 2026 and 2027 dates,
   with where and when they were read. Schedules are facts. A date on the site
   replaces the committed one for the same reference month.

``www.bls.gov`` refuses a plain automated client (see ``pmfa.world.evidence.bls``),
so in practice the committed file is what builds the BLS part. BLS also has
``.ics`` files under ``/schedule/``; they sit behind the same refusal and are not used.
The Public Data API (``api.bls.gov``) answered without a key, but it serves data
series, not release dates or release text, so it is not used either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from importlib import resources

from bs4 import BeautifulSoup

from pmfa.records.evidence import ScheduledEvent
from pmfa.world.evidence.common import STATEMENT_RELEASE, eastern_to_utc, to_utc
from pmfa.world.evidence.http import Fetcher, FetchError

FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_SCHEDULE_URLS = {
    "cpi_release": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "employment_situation": "https://www.bls.gov/schedule/news_release/empsit.htm",
}
BLS_TITLES = {
    "cpi_release": "Consumer Price Index",
    "employment_situation": "Employment Situation",
}

_MONTHS = {
    name: i
    for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


def _month_number(label: str) -> int:
    return _MONTHS[label.strip().lower()[:3]]


# -- FOMC ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FomcMeeting:
    start: date
    end: date
    """The decision day: the statement comes out on this date."""
    has_projections: bool


def parse_fomc_calendar(markup: str) -> list[FomcMeeting]:
    """Meetings with a policy decision from the FOMC calendar page, earliest first."""
    soup = BeautifulSoup(markup, "lxml")
    meetings: list[FomcMeeting] = []
    for panel in soup.select("div.panel"):
        heading = panel.select_one(".panel-heading")
        m = re.match(r"\s*(\d{4}) FOMC Meetings", heading.get_text(" ") if heading else "")
        if not m:
            continue
        year = int(m.group(1))
        for row in panel.select("div.fomc-meeting"):
            month_cell, date_cell = (
                row.select_one(".fomc-meeting__month"),
                row.select_one(".fomc-meeting__date"),
            )
            if month_cell is None or date_cell is None:
                continue
            if "Strategy Meeting" in row.get_text(" "):
                continue  # a discussion meeting: no decision, no statement
            months = [_month_number(p) for p in re.findall(r"[A-Za-z]{3,}", month_cell.get_text())]
            label = date_cell.get_text(" ")
            days = [int(d) for d in re.findall(r"\d+", label)]
            if not months or not days or "notation" in label.lower():
                continue
            start = date(year, months[0], days[0])
            end = date(year, months[-1], days[-1])
            meetings.append(FomcMeeting(start, end, "*" in label))
    return sorted(meetings, key=lambda mt: mt.end)


def fomc_events(meetings: Iterable[FomcMeeting]) -> list[ScheduledEvent]:
    """One ``fomc_decision`` event per meeting, at 2:00 p.m. Eastern on its last day."""
    events = []
    for mt in meetings:
        days = (
            f"{mt.start:%B} {mt.start.day}-{mt.end.day}"
            if mt.start.month == mt.end.month
            else (f"{mt.start:%B} {mt.start.day}-{mt.end:%B} {mt.end.day}")
        )
        events.append(
            ScheduledEvent(
                scheduled_event_id=f"fomc_decision:{mt.end.isoformat()}",
                kind="fomc_decision",
                title=f"FOMC meeting decision, {days}, {mt.end.year}",
                scheduled_at=eastern_to_utc(mt.end, STATEMENT_RELEASE),
            )
        )
    return events


# -- BLS -----------------------------------------------------------------------------------

_REFERENCE_MONTH = re.compile(r"^([A-Za-z]{3,9})\.? (\d{4})$")
_RELEASE_DATE = re.compile(r"^([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4})$")
_RELEASE_TIME = re.compile(r"^(\d{1,2}):(\d{2}) ?([AP])M$", re.IGNORECASE)


def parse_bls_schedule(markup: str) -> dict[str, tuple[date, time]]:
    """Rows of a BLS release-schedule table: ``{"2026-11": (date(2026, 12, 10), time(8, 30))}``.

    The table has three columns: reference month (``November 2026``), release
    date (``Dec. 10, 2026``) and release time (``08:30 AM``). Rows are found by
    what their cells look like, not by class names, so a restyled page still
    parses. A row that does not match is ignored.
    """
    soup = BeautifulSoup(markup, "lxml")
    out: dict[str, tuple[date, time]] = {}
    for tr in soup.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ")).strip() for c in tr.find_all(["td", "th"])]
        ref = next((m for c in cells if (m := _REFERENCE_MONTH.match(c))), None)
        day = next((m for c in cells if (m := _RELEASE_DATE.match(c))), None)
        at = next((m for c in cells if (m := _RELEASE_TIME.match(c))), None)
        if not (ref and day and at):
            continue
        try:
            period = f"{int(ref.group(2)):04d}-{_month_number(ref.group(1)):02d}"
            released = date(int(day.group(3)), _month_number(day.group(1)), int(day.group(2)))
        except (KeyError, ValueError):
            continue
        hour = int(at.group(1)) % 12 + (12 if at.group(3).upper() == "P" else 0)
        out[period] = (released, time(hour, int(at.group(2))))
    return out


def bls_events(kind: str, releases: dict[str, tuple[date, time]]) -> list[ScheduledEvent]:
    """Scheduled events for one BLS release kind from ``{period: (date, time)}``."""
    events = []
    for period, (day, at) in sorted(releases.items()):
        year, month = period.split("-")
        events.append(
            ScheduledEvent(
                scheduled_event_id=f"{kind}:{period}",
                kind=kind,
                title=f"{BLS_TITLES[kind]} for {date(int(year), int(month), 1):%B %Y}",
                scheduled_at=eastern_to_utc(day, at),
                period=period,
            )
        )
    return events


def committed_bls_schedule() -> dict[str, dict[str, tuple[date, time]]]:
    """The committed BLS dates, ``{kind: {period: (date, time)}}``, from ``bls_schedule.json``."""
    raw = json.loads(
        resources.files("pmfa.world.evidence").joinpath("bls_schedule.json").read_text("utf-8")
    )
    hour, minute = (int(p) for p in raw.get("time_eastern", "08:30").split(":"))
    at = time(hour, minute)
    return {
        kind: {p: (date.fromisoformat(d), at) for p, d in raw[kind].items()}
        for kind in BLS_SCHEDULE_URLS
    }


# -- the calendar --------------------------------------------------------------------------


class ReleaseCalendar:
    """Scheduled events, earliest first, with the window rule of the Evidence port."""

    def __init__(self, events: Iterable[ScheduledEvent]) -> None:
        unique = {e.scheduled_event_id: e for e in events}
        self._events = sorted(unique.values(), key=lambda e: (e.scheduled_at, e.scheduled_event_id))

    @property
    def events(self) -> list[ScheduledEvent]:
        return list(self._events)

    def between(self, start: datetime, end: datetime) -> list[ScheduledEvent]:
        """Entries with ``start < scheduled_at <= end``, earliest first."""
        start, end = to_utc(start), to_utc(end)
        return [e for e in self._events if start < e.scheduled_at <= end]


def build_calendar(
    fetcher: Fetcher | None = None,
    *,
    fomc_markup: str | None = None,
    use_fomc: bool = True,
    use_bls_site: bool = True,
) -> tuple[ReleaseCalendar, list[str]]:
    """The calendar and notes on where each part came from.

    ``fomc_markup`` is the FOMC calendar page if the caller already has it;
    otherwise it is fetched (``use_fomc=False`` leaves FOMC out). BLS dates start from the committed file; the BLS
    schedule pages are tried through ``fetcher`` (when ``use_bls_site``) and
    their dates replace the committed ones. A refusal from BLS is not an error:
    it is noted and the committed dates stand.
    """
    notes: list[str] = []
    events: list[ScheduledEvent] = []
    if use_fomc:
        if fomc_markup is None:
            if fetcher is None:
                raise ValueError("build_calendar needs the FOMC calendar page or a fetcher")
            fomc_markup = fetcher.get(FOMC_CALENDAR_URL).text
        events = fomc_events(parse_fomc_calendar(fomc_markup))
        notes.append(f"fomc: {FOMC_CALENDAR_URL}")

    schedule = committed_bls_schedule()
    notes.append("bls: committed bls_schedule.json")
    if use_bls_site and fetcher is not None:
        for kind, url in BLS_SCHEDULE_URLS.items():
            try:
                schedule[kind].update(parse_bls_schedule(fetcher.get(url).text))
                notes.append(f"bls: {url} (dates from the site replace the committed ones)")
            except FetchError as exc:
                notes.append(f"bls: {url} not available ({exc}); committed dates used")
    for kind, releases in schedule.items():
        events += bls_events(kind, releases)
    return ReleaseCalendar(events), notes
