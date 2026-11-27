"""Tree levels from the calendar, and the code checks that delete scenarios."""

from __future__ import annotations

from datetime import timedelta

from pmfa.config import Config
from pmfa.forecast import build_outcome_space, is_fast_path, levels
from pmfa.forecast.checks import calendar_check, evidence_check, rules_check
from pmfa.forecast.tree import make_node
from pmfa.records.evidence import ScheduledEvent
from pmfa.records.roles import ScheduledEventBrief

from .scripted import AS_OF, ORIGINS, PASSAGES, level, load_event, resolution, series, thought

FED = load_event("KXFEDDECISION-26DEC")  # closes 2026-12-09T18:59Z
FED_SPACE = build_outcome_space(FED, series("KXFEDDECISION"))
CPI = load_event("KXCPIYOY-26NOV")
CPI_SPACE = build_outcome_space(CPI, series("KXCPIYOY"))


def _cal(sid: str, days: float) -> ScheduledEvent:
    return ScheduledEvent(
        scheduled_event_id=sid,
        kind="cpi_release",
        title=sid,
        scheduled_at=AS_OF + timedelta(days=days),
    )


def test_levels_take_the_nearest_upcoming_events_then_the_resolution():
    calendar = [_cal("past", -1), _cal("c", 9), _cal("a", 3), _cal("b", 6), _cal("after_close", 30)]
    got = levels(FED, calendar, AS_OF, Config())
    assert [lv.scheduled_event_id for lv in got] == ["a", "b", "resolution:KXFEDDECISION-26DEC"]
    assert got[-1].is_resolution and not any(lv.is_resolution for lv in got[:-1])
    assert got[-1].scheduled_at == FED.close_time


def test_levels_without_calendar_entries_are_the_resolution_alone():
    got = levels(FED, [], AS_OF, Config())
    assert [lv.scheduled_event_id for lv in got] == ["resolution:KXFEDDECISION-26DEC"]


def test_an_event_closing_within_the_hour_gets_the_fast_path():
    as_of = FED.close_time - timedelta(minutes=30)
    assert is_fast_path(FED, as_of, Config())
    got = levels(FED, [_cal("soon", 0)], as_of, Config())
    assert len(got) == 1 and got[0].is_resolution
    assert not is_fast_path(FED, AS_OF, Config())


def _node(level_brief: ScheduledEventBrief, event_ticker: str = "KXFEDDECISION-26DEC", **kw):
    return make_node(
        episode_id="ep",
        event_ticker=event_ticker,
        parent=None,
        level=level_brief,
        depth=1,
        index=0,
        thought=thought("claim", **kw),
    )


def test_node_ids_differ_between_events_on_the_same_level():
    early = level("cpi", 5)
    first = _node(early, event_ticker="KXFEDDECISION-26DEC")
    second = _node(early, event_ticker="KXFEDDECISION-27JAN")
    assert first.node_id != second.node_id
    assert first.node_id == _node(early, event_ticker="KXFEDDECISION-26DEC").node_id


def test_rules_check():
    res, early = resolution(FED), level("cpi", 5)
    assert rules_check(_node(res, outcome_ids=("hold",)), res, FED_SPACE).passed
    assert not rules_check(_node(res), res, FED_SPACE).passed
    assert not rules_check(_node(res, outcome_ids=("pause",)), res, FED_SPACE).passed
    assert not rules_check(_node(early, outcome_ids=("hold",)), early, FED_SPACE).passed
    assert not rules_check(_node(early, low=3.0), early, FED_SPACE).passed
    assert rules_check(_node(early), early, FED_SPACE).passed

    cpi_res = resolution(CPI)
    assert rules_check(_node(cpi_res, low=3.5), cpi_res, CPI_SPACE).passed
    assert rules_check(_node(cpi_res, low=3.5, high=3.7), cpi_res, CPI_SPACE).passed
    assert not rules_check(_node(cpi_res, low=3.7, high=3.5), cpi_res, CPI_SPACE).passed
    assert not rules_check(_node(cpi_res), cpi_res, CPI_SPACE).passed
    assert not rules_check(_node(cpi_res, low=float("inf")), cpi_res, CPI_SPACE).passed


def test_calendar_check():
    close = FED.close_time
    assert calendar_check(level("x", 3), AS_OF, close).passed
    assert calendar_check(resolution(FED), AS_OF, close).passed
    assert not calendar_check(level("x", -1), AS_OF, close).passed
    assert not calendar_check(level("x", 30), AS_OF, close).passed


def test_evidence_check():
    by_id = {p.passage_id: p for p in PASSAGES}
    counted = {o.origin_id for o in ORIGINS}
    lv = level("x", 3)
    assert evidence_check(_node(lv, passages=("p1", "p2")), by_id, counted, AS_OF).passed
    assert not evidence_check(_node(lv, passages=()), by_id, counted, AS_OF).passed
    assert not evidence_check(_node(lv, passages=("p1", "nope")), by_id, counted, AS_OF).passed
    late = evidence_check(_node(lv, passages=("p_late",)), by_id, counted, AS_OF)
    assert not late.passed and "after the as-of time" in late.detail
    assert not evidence_check(_node(lv, passages=("p_orphan",)), by_id, counted, AS_OF).passed
