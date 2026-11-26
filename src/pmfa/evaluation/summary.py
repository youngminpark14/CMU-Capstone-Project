"""The tallies the report shows, computed from episode records.

Exits by reason, gate rejections by reason, routing tiers, and cost, calls, and
latency per episode; the first self-halt of a pass and what followed it; plus
the aggregate of the per-episode pruning audits. Most episodes are expected to
end in a no-trade exit, which is why the exit tally matters as much as the P&L.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from decimal import Decimal

from pmfa.records.base import Dollars, Record, UtcTime
from pmfa.records.episode import EpisodeRecord, Exit


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


class SelfHalt(Record):
    """The first episode of a pass that ended ``halted``, and how the pass went from then on.

    Nobody resumes a halt in a backtest, so once a stop condition trips, every
    later episode still forecasts but ends ``halted`` at the gate and sends no
    order. A report needs this said, not only counted.
    """

    first_episode_id: str
    first_trigger_id: str
    first_started_at: UtcTime
    """When the first halted episode started, on the pass's simulated clock."""
    reason: str
    """That episode's exit detail: the stop condition that tripped."""
    n_from_then: int
    """Episodes from the first halted one on, that one included."""
    n_halted_from_then: int
    """Of those, the episodes that ended ``halted``."""


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


def self_halt(episodes: Sequence[EpisodeRecord]) -> SelfHalt | None:
    """The first ``halted`` exit of one pass's episodes and what followed; ``None`` with none.

    ``episodes`` must be in the order the pass ran them; they are taken as
    given. No field of the record can recover that order: two late trading
    episodes that waited for the same order outcome share a start and an as-of
    time, and the episode id is a hash. Only the run knows it.
    """
    first = next((i for i, e in enumerate(episodes) if e.exit is Exit.HALTED), None)
    if first is None:
        return None
    head, later = episodes[first], episodes[first:]
    return SelfHalt(
        first_episode_id=head.episode_id,
        first_trigger_id=head.trigger.trigger_id,
        first_started_at=head.started_at,
        reason=head.exit_detail or "no reason recorded",
        n_from_then=len(later),
        n_halted_from_then=sum(e.exit is Exit.HALTED for e in later),
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
