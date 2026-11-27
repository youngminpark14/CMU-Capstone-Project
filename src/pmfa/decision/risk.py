"""Risk state: day P&L, drawdown, and the two stop conditions, from an equity series.

Two bases, kept apart on purpose. ``RiskState.bankroll`` is the current equity:
the dollar caps are fractions of it, the same number sizing caps on, so the gate
never rejects at a cap what sizing put exactly at it. The daily loss stop is a
fraction of ``RiskState.day_start_equity``, so one day has one stop level
however equity moves during it (the gate, the self-halt check, and the stage
manager all count the same stop).

The trading day is the calendar day in the configured time zone. The daily
loss stop is sticky: once equity has fallen far enough below the day's start
at any moment of the day, it stays tripped until the next trading day, even if
equity recovers. The drawdown halt reflects the current drawdown; keeping the
agent halted afterwards is the persisted halt flag's job, not this function's.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from pmfa.config import Config
from pmfa.records.base import ZERO
from pmfa.records.episode import RiskState


def trading_day_start(now: datetime, timezone: str) -> datetime:
    """Midnight of ``now``'s calendar day in ``timezone``, as UTC."""
    if now.tzinfo is None:
        raise ValueError("naive datetime: pass a timezone-aware time")
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    return datetime.combine(local.date(), time(0), tzinfo=zone).astimezone(UTC)


def risk_state(
    equity_series: Sequence[tuple[datetime, Decimal]],
    now: datetime,
    config: Config,
    *,
    bankroll: Decimal | None = None,
) -> RiskState:
    """The risk state at ``now`` from ``(timestamp, equity)`` points.

    Points after ``now`` are ignored. ``bankroll`` is the current equity the dollar
    caps are fractions of (pass the executor's, the number sizing uses); it defaults
    to the last point at or before ``now``. It does not move the daily loss stop,
    which is always a fraction of the day-start equity: the last point before the
    trading day began, else the day's first point.
    """
    day_start = trading_day_start(now, config.oversight.timezone)
    points = sorted((t, Decimal(e)) for t, e in equity_series if t <= now)
    if not points:
        if bankroll is None:
            raise ValueError("an empty equity series needs an explicit bankroll")
        return RiskState(
            bankroll=bankroll,
            day_start_equity=bankroll,
            peak_equity=bankroll,
            day_pnl=ZERO,
            drawdown_fraction=ZERO,
            loss_stop_tripped=False,
            drawdown_tripped=False,
        )

    before_day = [e for t, e in points if t < day_start]
    today = [e for t, e in points if t >= day_start]
    start_equity = before_day[-1] if before_day else today[0]
    current = points[-1][1]
    peak = max(e for _, e in points)
    bank = current if bankroll is None else bankroll

    stop = config.caps.daily_loss_stop_fraction * start_equity
    loss_tripped = any(e - start_equity <= -stop for e in today) if stop > 0 else False
    drawdown = (peak - current) / peak if peak > 0 else ZERO
    return RiskState(
        bankroll=bank,
        day_start_equity=start_equity,
        peak_equity=peak,
        day_pnl=current - start_equity,
        drawdown_fraction=max(ZERO, drawdown),
        loss_stop_tripped=loss_tripped,
        drawdown_tripped=drawdown >= config.caps.drawdown_halt_fraction,
    )
