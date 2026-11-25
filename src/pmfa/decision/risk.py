"""Risk state: day P&L, drawdown, and the two stop conditions, from an equity series.

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

    Points after ``now`` are ignored. ``bankroll`` sizes the daily loss stop;
    it defaults to the day's starting equity. Day-start equity is the last point
    before the trading day began, else the day's first point.
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
    bank = start_equity if bankroll is None else bankroll

    stop = config.caps.daily_loss_stop_fraction * bank
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
