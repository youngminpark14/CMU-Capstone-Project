"""Clocks. Live modes read the system clock; replay and backtests run on a simulated one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class SystemClock:
    """The real time, in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    """A clock that only moves when told to. Replay, backtests, and tests use it."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("a simulated clock needs a timezone-aware start time")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("a simulated clock needs timezone-aware times")
        self._now = moment.astimezone(UTC)

    def advance(self, delta: timedelta | float) -> None:
        """Move forward by a timedelta, or by a number of seconds."""
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
        if step < timedelta(0):
            raise ValueError("a clock cannot move backwards")
        self._now += step
