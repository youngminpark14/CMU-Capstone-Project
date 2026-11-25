"""The levels of the scenario tree: scheduled events between the as-of time and the close.

Each level is one scheduled event the generator makes a claim about. Up to
``max_depth - 1`` of the nearest calendar entries strictly between the as-of
time and the event's close come first; the last level is always the event's own
resolution. An event that closes within ``fast_path_within_minutes`` gets the
fast path: the resolution level alone, generated and scored on the fast-path slot.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pmfa.config import Config
from pmfa.records.evidence import ScheduledEvent
from pmfa.records.market import Event
from pmfa.records.roles import ScheduledEventBrief

RESOLUTION_PREFIX = "resolution:"


def resolution_level_id(event_ticker: str) -> str:
    return RESOLUTION_PREFIX + event_ticker


def resolution_level(event: Event) -> ScheduledEventBrief:
    return ScheduledEventBrief(
        scheduled_event_id=resolution_level_id(event.event_ticker),
        title=f"Resolution: {event.title}",
        scheduled_at=event.close_time,
        is_resolution=True,
    )


def is_fast_path(event: Event, as_of: datetime, config: Config) -> bool:
    """True when the event closes within the configured fast-path window of ``as_of``."""
    return event.close_time - as_of <= timedelta(minutes=config.forecast.fast_path_within_minutes)


def levels(
    event: Event,
    scheduled_events: list[ScheduledEvent],
    as_of: datetime,
    config: Config,
) -> list[ScheduledEventBrief]:
    """The tree's levels, root first; the resolution level is always last."""
    if is_fast_path(event, as_of, config):
        return [resolution_level(event)]
    close = event.close_time
    upcoming = sorted(
        (s for s in scheduled_events if as_of < s.scheduled_at < close),
        key=lambda s: (s.scheduled_at, s.scheduled_event_id),
    )
    chosen = upcoming[: max(0, config.forecast.max_depth - 1)]
    return [
        *(
            ScheduledEventBrief(
                scheduled_event_id=s.scheduled_event_id,
                title=s.title,
                scheduled_at=s.scheduled_at,
                is_resolution=False,
            )
            for s in chosen
        ),
        resolution_level(event),
    ]
