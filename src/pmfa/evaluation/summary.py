"""The tallies the report shows, computed from episode records.

Exits by reason, gate rejections by reason, routing tiers, and cost, calls, and
latency per episode; plus the aggregate of the per-episode pruning audits.
Most episodes are expected to end in a no-trade exit, which is why the exit
tally matters as much as the P&L.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from decimal import Decimal

from pmfa.records.base import Dollars, Record
from pmfa.records.episode import EpisodeRecord


class Spread(Record):
    """Mean, median, and maximum of a per-episode quantity; ``None`` with no episodes."""

    n: int
    mean: float | None
    median: float | None
    max: float | None


class DollarSpread(Record):
    """The same, for dollars."""

    n: int
    mean: Dollars | None
    median: Dollars | None
    max: Dollars | None


class EpisodeTallies(Record):
    n_episodes: int
    exits: dict[str, int]
    """Episodes per exit; an episode that has not ended counts under ``unfinished``."""
    gate_rejections: dict[str, int]
    """Failed gate checks over rejected proposals. One rejection can fail several checks."""
    n_gate_rejected: int
    routing_tiers: dict[str, int]
    n_would_be_auto: int
    """Proposals that met every auto condition but went to review because of the stage."""
    cost_usd: DollarSpread
    calls: Spread
    latency_seconds: Spread
    """Model-call seconds per episode, as the gateway meters them."""


class PruningSummary(Record):
    """How often the path that happened ran through a scenario the search had frozen."""

    n_episodes: int
    mean_frozen_posterior_mass: float | None
    """Mean share of the posterior (given the outcome) on frozen scenarios."""
    frozen_most_likely_rate: float | None
    """Share of episodes whose most likely scenario given the outcome was frozen."""


def _counts(values: Iterable[str]) -> dict[str, int]:
    """Counts, most common first, ties by name, so tables read the same on every run."""
    return dict(sorted(Counter(values).items(), key=lambda kv: (-kv[1], kv[0])))


def spread(values: Sequence[float]) -> Spread:
    """Mean, median, and maximum of a list of numbers."""
    if not values:
        return Spread(n=0, mean=None, median=None, max=None)
    return Spread(
        n=len(values),
        mean=statistics.fmean(values),
        median=float(statistics.median(values)),
        max=float(max(values)),
    )


def dollar_spread(values: Sequence[Decimal]) -> DollarSpread:
    """Mean, median, and maximum of a list of dollar amounts, in decimals."""
    if not values:
        return DollarSpread(n=0, mean=None, median=None, max=None)
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return DollarSpread(
        n=len(values),
        mean=sum(values, Decimal("0")) / len(values),
        median=median,
        max=ordered[-1],
    )


def episode_tallies(episodes: Sequence[EpisodeRecord]) -> EpisodeTallies:
    """Exits, gate rejections, routing tiers, and per-episode cost, calls, and latency."""
    rejected = [e.gate for e in episodes if e.gate is not None and not e.gate.approved]
    routed = [e.routing for e in episodes if e.routing is not None]
    return EpisodeTallies(
        n_episodes=len(episodes),
        exits=_counts(e.exit.value if e.exit is not None else "unfinished" for e in episodes),
        gate_rejections=_counts(reason for g in rejected for reason in g.reasons),
        n_gate_rejected=len(rejected),
        routing_tiers=_counts(r.tier.value for r in routed),
        n_would_be_auto=sum(r.would_be_auto for r in routed),
        cost_usd=dollar_spread([e.usage.cost_usd for e in episodes]),
        calls=spread([float(e.usage.calls) for e in episodes]),
        latency_seconds=spread([e.usage.seconds for e in episodes]),
    )


def pruning_summary(audits: Iterable[tuple[float, bool]]) -> PruningSummary:
    """Aggregate per-episode pruning audits.

    Each item is ``(frozen posterior mass, most likely scenario was frozen)``
    for one resolved episode, as the forecast module's audit reports them.
    """
    items = list(audits)
    if not items:
        return PruningSummary(
            n_episodes=0, mean_frozen_posterior_mass=None, frozen_most_likely_rate=None
        )
    for mass, _ in items:
        if not (0.0 <= mass <= 1.0):
            raise ValueError(f"frozen posterior mass {mass} is outside [0, 1]")
    return PruningSummary(
        n_episodes=len(items),
        mean_frozen_posterior_mass=statistics.fmean(m for m, _ in items),
        frozen_most_likely_rate=sum(f for _, f in items) / len(items),
    )
