"""From five critic quantiles to a probability for every ladder bin and market.

The critic gives the 5th, 25th, 50th, 75th and 95th percentiles of the number a
ladder event resolves on. Code turns them into one continuous CDF ``F`` of the
unrounded value:

- **Between the quantiles** ``F`` is linear (piecewise-uniform density).
- **Tail rule.** Beyond the outer quantiles ``F`` has an exponential tail whose
  density at the outer quantile equals the density of the adjacent segment:

      upper: 1 - F(x) = 0.05 * exp(-lam_u * (x - q95)),  lam_u = d_u / 0.05
      lower:     F(x) = 0.05 * exp(-lam_l * (q05 - x)),  lam_l = d_l / 0.05

  where ``d_u`` is the density of the last segment of positive width that ends
  at ``q95`` (normally ``(0.95 - 0.75) / (q95 - q75)``) and ``d_l`` that of the
  first one that starts at ``q05``. The tails carry exactly 5% each.
- **Equal neighbouring quantiles** are a point mass: ``F`` jumps there and is
  right-continuous. Zero-width segments are skipped, never divided by. When all
  five quantiles are equal, the tails use a segment of one ``fallback_width``
  (the series increment).

**Reporting steps.** The resolving number is reported in steps of the series
increment ``d`` (0.1 for CPI). A reported value above 3.6 means the unrounded
value is at least 3.65, so a bin ``(low, high]`` of reported values has
probability ``F(high + d/2) - F(low + d/2)``. The outcome space already folded
strict and inclusive comparisons into ``(low, high]`` bins (see
``outcomes.yes_interval``), so this one shift covers both.

**Never 0 or 1.** Every bin gets at least ``MIN_BIN_PROBABILITY`` and the bins are
renormalised, so a market (a proper subset of bins) is strictly inside (0, 1).
Markets that are nested thresholds get monotone probabilities, because each is a
sum over a superset of the next one's bins.
"""

from __future__ import annotations

import bisect
import math

from pmfa.records.forecast import OutcomeSpace
from pmfa.records.roles import Quantiles

QUANTILE_LEVELS: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 0.95)
MIN_BIN_PROBABILITY = 1e-9


class QuantileCdf:
    """A continuous CDF through five quantiles, with exponential tails."""

    def __init__(
        self,
        quantiles: Quantiles,
        *,
        fallback_width: float = 1.0,
        levels: tuple[float, ...] = QUANTILE_LEVELS,
    ) -> None:
        self.xs = [quantiles.q05, quantiles.q25, quantiles.q50, quantiles.q75, quantiles.q95]
        self.ps = list(levels)
        if len(self.ps) != len(self.xs):
            raise ValueError("one level per quantile")
        width = fallback_width if fallback_width > 0 else 1.0
        self.lower_density = self._edge_density(lower=True, fallback_width=width)
        self.upper_density = self._edge_density(lower=False, fallback_width=width)
        self.lower_rate = self.lower_density / self.ps[0]
        self.upper_rate = self.upper_density / (1.0 - self.ps[-1])

    def _edge_density(self, *, lower: bool, fallback_width: float) -> float:
        xs, ps = self.xs, self.ps
        if lower:
            for j in range(1, len(xs)):
                if xs[j] > xs[0]:
                    return (ps[j] - ps[j - 1]) / (xs[j] - xs[j - 1])
            return (ps[1] - ps[0]) / fallback_width
        for j in range(len(xs) - 2, -1, -1):
            if xs[j] < xs[-1]:
                return (ps[j + 1] - ps[j]) / (xs[j + 1] - xs[j])
        return (ps[-1] - ps[-2]) / fallback_width

    def __call__(self, x: float) -> float:
        """``P(value <= x)``."""
        if math.isinf(x):
            return 0.0 if x < 0 else 1.0
        xs, ps = self.xs, self.ps
        k = bisect.bisect_right(xs, x)
        if k == 0:
            return ps[0] * math.exp(-self.lower_rate * (xs[0] - x))
        if k == len(xs):
            return 1.0 - (1.0 - ps[-1]) * math.exp(-self.upper_rate * (x - xs[-1]))
        lo, hi = xs[k - 1], xs[k]
        return ps[k - 1] + (ps[k] - ps[k - 1]) * (x - lo) / (hi - lo)


def bin_probabilities(space: OutcomeSpace, quantiles: Quantiles) -> dict[str, float]:
    """The probability of every ladder bin, from five quantiles of the resolving number."""
    if space.kind != "ladder":
        raise ValueError("quantiles describe ladder events only")
    increment = space.increment or 0.0
    cdf = QuantileCdf(quantiles, fallback_width=increment or 1.0)
    half = increment / 2.0
    raw: dict[str, float] = {}
    for outcome in space.outcomes:
        upper = cdf(outcome.high + half) if outcome.high is not None else 1.0
        lower = cdf(outcome.low + half) if outcome.low is not None else 0.0
        raw[outcome.outcome_id] = max(upper - lower, MIN_BIN_PROBABILITY)
    total = sum(raw.values())
    return {oid: p / total for oid, p in raw.items()}


def market_probabilities(
    space: OutcomeSpace, outcome_probabilities: dict[str, float]
) -> dict[str, float]:
    """Each market's YES probability: the sum over the outcomes on which it resolves YES."""
    return {
        ticker: min(1.0, sum(outcome_probabilities.get(oid, 0.0) for oid in oids))
        for ticker, oids in space.market_outcomes.items()
    }
