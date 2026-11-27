"""The release calendar: FOMC decision days, BLS release dates, window bounds."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from pmfa.world.evidence import calendar as cal

from .conftest import FED, FakeFetcher, fixture_text


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def fomc_events():
    return cal.fomc_events(cal.parse_fomc_calendar(fixture_text("fomc_calendar.html")))


def test_fomc_2026_meetings_include_the_known_dates():
    meetings = cal.parse_fomc_calendar(fixture_text("fomc_calendar.html"))
    by_end = {m.end: m for m in meetings if m.end.year == 2026}
    assert len(by_end) == 8
    for start, end in [
        (date(2026, 7, 28), date(2026, 7, 29)),
        (date(2026, 9, 15), date(2026, 9, 16)),
        (date(2026, 10, 27), date(2026, 10, 28)),
        (date(2026, 12, 8), date(2026, 12, 9)),
    ]:
        assert by_end[end].start == start
    assert (
        by_end[date(2026, 9, 16)].has_projections and not by_end[date(2026, 10, 28)].has_projections
    )
    # The October 2027 row is a strategy meeting without a decision.
    assert date(2027, 10, 27) not in {m.end for m in meetings}


def test_fomc_events_have_stable_ids_and_statement_times_in_both_clock_regimes():
    events = {e.scheduled_event_id: e for e in fomc_events()}
    summer = events["fomc_decision:2026-10-28"]
    winter = events["fomc_decision:2026-12-09"]
    assert summer.kind == "fomc_decision" and summer.scheduled_at == utc(2026, 10, 28, 18)  # EDT
    assert winter.scheduled_at == utc(2026, 12, 9, 19)  # EST
    assert "December 8-9, 2026" in winter.title and winter.period == ""
    assert events["fomc_decision:2026-01-28"].scheduled_at == utc(2026, 1, 28, 19)


def test_committed_bls_dates_and_times():
    schedule = cal.committed_bls_schedule()
    events = {
        e.scheduled_event_id: e
        for kind, releases in schedule.items()
        for e in cal.bls_events(kind, releases)
    }
    cpi_nov = events["cpi_release:2026-11"]
    assert cpi_nov.scheduled_at == utc(2026, 12, 10, 13, 30)  # 8:30 a.m. EST
    assert cpi_nov.period == "2026-11" and cpi_nov.title == "Consumer Price Index for November 2026"
    assert events["employment_situation:2026-11"].scheduled_at == utc(2026, 12, 4, 13, 30)
    assert events["cpi_release:2026-09"].scheduled_at == utc(2026, 10, 14, 12, 30)  # 8:30 a.m. EDT
    assert events["employment_situation:2026-10"].scheduled_at == utc(2026, 11, 6, 13, 30)
    # 2026 and early 2027 are both there.
    assert "cpi_release:2026-01" in events and "employment_situation:2027-02" in events


# A table in the layout of the BLS release-schedule pages (reference month, release date, time).
SCHEDULE_TABLE = """<table class="release-list"><thead><tr><th>Reference Month</th>
<th>Release Date</th><th>Release Time</th></tr></thead><tbody>
<tr><td>October 2026</td><td>Nov. 10, 2026</td><td>08:30 AM</td></tr>
<tr><td>November 2026</td><td>Dec. 11, 2026</td><td>08:30 AM</td></tr>
<tr><td>Sept 2027</td><td>not a date</td><td>08:30 AM</td></tr>
</tbody></table>"""


def test_bls_schedule_table_parsing_and_override():
    parsed = cal.parse_bls_schedule(SCHEDULE_TABLE)
    assert parsed == {
        "2026-10": (date(2026, 11, 10), time(8, 30)),
        "2026-11": (date(2026, 12, 11), time(8, 30)),
    }
    fetcher = FakeFetcher(
        {
            cal.BLS_SCHEDULE_URLS["cpi_release"]: SCHEDULE_TABLE,
            f"{FED}/monetarypolicy/fomccalendars.htm": fixture_text("fomc_calendar.html"),
        },
        refused=(),
    )
    calendar, notes = cal.build_calendar(fetcher)
    cpi = {e.scheduled_event_id: e for e in calendar.events if e.kind == "cpi_release"}
    assert cpi["cpi_release:2026-11"].scheduled_at == utc(2026, 12, 11, 13, 30)  # the site wins
    assert cpi["cpi_release:2026-12"].scheduled_at == utc(2027, 1, 13, 13, 30)  # committed fills in
    assert any("employment" in n or "empsit" in n for n in notes)


def test_build_calendar_when_bls_refuses_uses_the_committed_file():
    fetcher = FakeFetcher(
        {f"{FED}/monetarypolicy/fomccalendars.htm": fixture_text("fomc_calendar.html")},
        refused=("bls.gov",),
    )
    calendar, notes = cal.build_calendar(fetcher)
    assert any("not available" in n for n in notes)
    assert any(e.scheduled_event_id == "cpi_release:2026-11" for e in calendar.events)
    assert any(e.scheduled_event_id == "fomc_decision:2026-12-09" for e in calendar.events)


def test_between_excludes_start_and_includes_end():
    calendar = cal.ReleaseCalendar(fomc_events())
    at = utc(2026, 10, 28, 18)
    assert [e.scheduled_event_id for e in calendar.between(utc(2026, 10, 1), at)] == [
        "fomc_decision:2026-10-28"
    ]
    assert calendar.between(at, utc(2026, 11, 30)) == []  # start is exclusive
    wide = calendar.between(utc(2026, 7, 1), utc(2026, 12, 31))
    assert [e.scheduled_event_id for e in wide] == [
        "fomc_decision:2026-07-29",
        "fomc_decision:2026-09-16",
        "fomc_decision:2026-10-28",
        "fomc_decision:2026-12-09",
    ]
