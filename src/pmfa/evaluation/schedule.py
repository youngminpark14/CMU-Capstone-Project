"""What a backtest runs: scoring points for the forecast metrics and triggers for the trading simulation.

- **Scoring points.** For every settled event of an enabled series whose close
  falls in the window, one point per horizon in ``config.evaluation.horizons_days``
  (7, 3 and 1 days before close). A point is skipped, with the reason recorded,
  when it falls before the window start or before the event opened.
- **Trading triggers.** Every corpus document published in the window, in time
  order, as a ``new_document`` trigger at its publish time.

Both lists are deterministic: the same snapshot and corpus give the same lists
in the same order. ``limit`` keeps an evenly spaced subsample of each, and the
schedule names everything it left out, so a capped run never hides what it
did not score.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from pmfa.config import Config
from pmfa.records.base import Record, UtcTime
from pmfa.records.evidence import ScheduledEvent, Trigger, TriggerKind
from pmfa.records.market import Event
from pmfa.world.evidence.corpus import CorpusStore


class ScoringPoint(Record):
    """One event at one horizon: every system forecasts every listed market of the event as of ``as_of``."""

    point_id: str
    event_ticker: str
    series_ticker: str
    category: str
    horizon_days: int
    as_of: UtcTime
    close_time: UtcTime

    def trigger(self) -> Trigger:
        """The schedule trigger the pipeline runs for this point."""
        return Trigger(
            trigger_id=f"schedule:{self.event_ticker}:{self.horizon_days}d",
            kind=TriggerKind.SCHEDULE,
            at=self.as_of,
            event_ticker=self.event_ticker,
            detail=f"{self.horizon_days} day(s) before close",
        )


class Schedule(Record):
    """The points and triggers of one run, and what was skipped or left out."""

    window_start: UtcTime
    window_end: UtcTime
    points: list[ScoringPoint]
    triggers: list[Trigger]
    skipped_points: list[str] = []
    """Points that do not exist, with why: before the window start or before the event opened."""
    skipped_events: list[str] = []
    """Events not scored, with why: not settled, or closing outside the window."""
    left_out_points: list[str] = []
    """Points dropped by ``limit``."""
    left_out_triggers: list[str] = []
    """Triggers dropped by ``limit``."""
    limit: int | None = None


def window_start_time(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


def subsample[T](items: list[T], limit: int | None) -> tuple[list[T], list[T]]:
    """An evenly spaced subsample of ``limit`` items (first and last included), and the rest.

    ``None`` or a limit at least as long as the list keeps everything.
    """
    if limit is None or limit >= len(items):
        return list(items), []
    if limit < 1:
        return [], list(items)
    if limit == 1:
        picked = {0}
    else:
        picked = {round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)}
    kept = [x for i, x in enumerate(items) if i in picked]
    dropped = [x for i, x in enumerate(items) if i not in picked]
    return kept, dropped


def is_settled(event: Event) -> bool:
    return bool(event.markets) and all(m.result is not None for m in event.markets)


def scoring_points(
    events: list[Event], config: Config, *, window_start: datetime, window_end: datetime
) -> tuple[list[ScoringPoint], list[str], list[str]]:
    """Points in time order, the points skipped with reasons, and the events skipped with reasons."""
    points: list[ScoringPoint] = []
    skipped: list[str] = []
    skipped_events: list[str] = []
    for event in events:
        sc = config.follow.for_ticker(event.series_ticker)
        if sc is None or not sc.enabled:
            continue
        close = event.close_time
        if not (window_start <= close <= window_end):
            skipped_events.append(
                f"{event.event_ticker}: closes {close.isoformat()}, outside the window"
            )
            continue
        if not is_settled(event):
            skipped_events.append(f"{event.event_ticker}: not settled in the snapshot")
            continue
        for horizon in sorted(set(config.evaluation.horizons_days), reverse=True):
            as_of = close - timedelta(days=horizon)
            pid = f"{event.event_ticker}@{horizon}d"
            if as_of < window_start:
                skipped.append(f"{pid}: {as_of.isoformat()} is before the window start")
                continue
            if as_of < event.open_time:
                skipped.append(f"{pid}: {as_of.isoformat()} is before the event opened")
                continue
            points.append(
                ScoringPoint(
                    point_id=pid,
                    event_ticker=event.event_ticker,
                    series_ticker=event.series_ticker,
                    category=sc.category,
                    horizon_days=horizon,
                    as_of=as_of,
                    close_time=close,
                )
            )
    points.sort(key=lambda p: (p.as_of, p.event_ticker))
    return points, skipped, skipped_events


def trading_triggers(
    corpus: CorpusStore, *, window_start: datetime, window_end: datetime
) -> list[Trigger]:
    """One ``new_document`` trigger per corpus document published in the window, oldest first."""
    return [
        Trigger(
            trigger_id=f"doc:{doc.doc_id}",
            kind=TriggerKind.NEW_DOCUMENT,
            at=doc.published_at,
            doc_id=doc.doc_id,
            detail=doc.title,
        )
        for doc in corpus.documents()
        if window_start <= doc.published_at <= window_end
    ]


RELEASE_KINDS = {
    "fed_decision": "fomc_decision",
    "fed_rate": "fomc_decision",
    "cpi": "cpi_release",
    "payrolls": "employment_situation",
}
"""Series category -> the release-calendar kind its events resolve on."""

RELEASE_MATCH = timedelta(days=2)
"""An event belongs to a release when it closes this close to the release time."""


def releases_without_event(
    events: list[Event],
    calendar: list[ScheduledEvent],
    config: Config,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[str]:
    """Releases in the window for which the snapshot holds no event of the followed series.

    The exchange does not list an event for every release (there is no
    KXCPIYOY event for the October 2026 CPI release, for example), and a
    snapshot can also miss one. Either way the window's scored events are
    fewer than its releases, and this names each such release so a report
    cannot imply otherwise.
    """
    out: list[str] = []
    for sc in config.follow.enabled_series():
        kind = RELEASE_KINDS.get(sc.category)
        if kind is None:
            continue
        closes = [e.close_time for e in events if e.series_ticker == sc.ticker and e.markets]
        for release in calendar:
            if release.kind != kind or not (window_start <= release.scheduled_at <= window_end):
                continue
            if any(abs(c - release.scheduled_at) <= RELEASE_MATCH for c in closes):
                continue
            out.append(
                f"{release.scheduled_event_id} ({release.title}, "
                f"{release.scheduled_at.isoformat()}): no {sc.ticker} event in the snapshot"
            )
    return out


def build_schedule(
    events: list[Event],
    corpus: CorpusStore,
    config: Config,
    *,
    window_end: datetime,
    limit: int | None = None,
) -> Schedule:
    """The run's schedule from the snapshot's events and the corpus, with ``limit`` applied to each list."""
    start = window_start_time(config.evaluation.window_start)
    points, skipped, skipped_events = scoring_points(
        events, config, window_start=start, window_end=window_end
    )
    triggers = trading_triggers(corpus, window_start=start, window_end=window_end)
    kept_points, dropped_points = subsample(points, limit)
    kept_triggers, dropped_triggers = subsample(triggers, limit)
    return Schedule(
        window_start=start,
        window_end=window_end,
        points=kept_points,
        triggers=kept_triggers,
        skipped_points=skipped,
        skipped_events=skipped_events,
        left_out_points=[p.point_id for p in dropped_points],
        left_out_triggers=[t.trigger_id for t in dropped_triggers],
        limit=limit,
    )


__all__ = [
    "RELEASE_KINDS",
    "Schedule",
    "ScoringPoint",
    "build_schedule",
    "is_settled",
    "releases_without_event",
    "scoring_points",
    "subsample",
    "trading_triggers",
    "window_start_time",
]
