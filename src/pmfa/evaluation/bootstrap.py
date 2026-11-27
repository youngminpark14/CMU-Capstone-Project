"""Bootstrap confidence intervals clustered by event.

Markets within one event are correlated: a Fed meeting has five markets and one
CPI event carried 21, and they all resolve on the same number. Resampling
markets would treat them as independent and overstate the sample, so every
interval here resamples whole events (clusters) with replacement and keeps each
event's rows together. With fewer than two events there is nothing to resample
between, and the bounds are ``None`` rather than a made-up interval.

The interval is the percentile interval of the resampled statistic. Draws use
NumPy's seeded generator, so the same seed gives the same interval.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from decimal import Decimal

from pmfa.records.base import Dollars, Record

from .metrics import ScoredRow, brier_of, scored_rows, skill_of
from .pnl import trade_pnl
from .rows import ForecastRow, TradeRow


class Interval(Record):
    """A point estimate with a clustered bootstrap interval."""

    estimate: float | None
    lower: float | None
    """``None`` with fewer than two clusters, or when no resample had a defined statistic."""
    upper: float | None
    n_clusters: int
    n_rows: int
    level: float
    samples: int
    seed: int


class DollarInterval(Record):
    """The same, for a dollar amount."""

    estimate: Dollars
    lower: Dollars | None
    upper: Dollars | None
    n_clusters: int
    n_rows: int
    level: float
    samples: int
    seed: int


def _clusters[R](rows: Sequence[R], key: Callable[[R], str]) -> list[list[R]]:
    groups: dict[str, list[R]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return [groups[k] for k in sorted(groups)]


def _quantile[V: (float, Decimal)](values: list[V], q: float) -> V:
    """Linear-interpolation quantile of sorted values (NumPy's default method)."""
    h = (len(values) - 1) * q
    i = math.floor(h)
    if i + 1 >= len(values):
        return values[-1]
    lo, hi = values[i], values[i + 1]
    if isinstance(lo, Decimal):
        return lo + (hi - lo) * Decimal(str(h - i))
    return lo + (hi - lo) * (h - i)


def _resample[R, V: (float, Decimal)](
    clusters: list[list[R]],
    statistic: Callable[[list[R]], V | None],
    *,
    samples: int,
    seed: int,
    level: float,
) -> tuple[V | None, V | None]:
    """Percentile bounds of ``statistic`` over cluster resamples; ``None`` when undefined."""
    import numpy as np

    if not (0.0 < level < 1.0):
        raise ValueError("level must be in (0, 1)")
    if samples < 1:
        raise ValueError("samples must be at least 1")
    if len(clusters) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(clusters), size=(samples, len(clusters)))
    values: list[V] = []
    for row in draws:
        value = statistic([r for idx in row for r in clusters[idx]])
        if value is not None:
            values.append(value)
    if not values:
        return None, None
    values.sort()
    tail = (1.0 - level) / 2.0
    return _quantile(values, tail), _quantile(values, 1.0 - tail)


def _forecast_interval(
    rows: Sequence[ForecastRow],
    statistic: Callable[[Sequence[ScoredRow]], float | None],
    *,
    seed: int,
    samples: int,
    level: float,
    abstain_as_market: bool,
) -> Interval:
    scored = scored_rows(rows, abstain_as_market=abstain_as_market)
    clusters = _clusters(scored, lambda s: s.event_ticker)
    lower, upper = _resample(clusters, statistic, samples=samples, seed=seed, level=level)
    return Interval(
        estimate=statistic(scored),
        lower=lower,
        upper=upper,
        n_clusters=len(clusters),
        n_rows=len(scored),
        level=level,
        samples=samples,
        seed=seed,
    )


def brier_interval(
    rows: Sequence[ForecastRow],
    *,
    seed: int,
    samples: int = 2000,
    level: float = 0.95,
    abstain_as_market: bool = False,
) -> Interval:
    """Mean Brier score with an interval that resamples events."""
    return _forecast_interval(
        rows,
        brier_of,
        seed=seed,
        samples=samples,
        level=level,
        abstain_as_market=abstain_as_market,
    )


def skill_interval(
    rows: Sequence[ForecastRow],
    *,
    seed: int,
    samples: int = 2000,
    level: float = 0.95,
    abstain_as_market: bool = False,
) -> Interval:
    """Brier skill against the market with an interval that resamples events.

    Each resample computes skill on its own rows, agent and market alike. A
    resample whose market Brier is zero has no skill and is left out.
    """
    return _forecast_interval(
        rows,
        skill_of,
        seed=seed,
        samples=samples,
        level=level,
        abstain_as_market=abstain_as_market,
    )


def pnl_interval(
    trades: Sequence[TradeRow],
    *,
    seed: int,
    samples: int = 2000,
    level: float = 0.95,
) -> DollarInterval:
    """Total realized P&L after fees with an interval that resamples events.

    Only settled trades count, as in ``pnl_summary``; ``n_rows`` is their number.
    """
    settled = [(t.event_ticker, pnl) for t in trades if (pnl := trade_pnl(t)) is not None]
    clusters = _clusters(settled, lambda s: s[0])

    def total(rows: Sequence[tuple[str, Decimal]]) -> Decimal:
        return sum((pnl for _, pnl in rows), Decimal("0"))

    lower, upper = _resample(clusters, total, samples=samples, seed=seed, level=level)
    return DollarInterval(
        estimate=total(settled),
        lower=lower,
        upper=upper,
        n_clusters=len(clusters),
        n_rows=len(settled),
        level=level,
        samples=samples,
        seed=seed,
    )
