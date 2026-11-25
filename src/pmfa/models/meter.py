"""Per-episode and per-run meters for model calls, with the hard caps from ``config.budget``.

The meter is checked before every call, so a capped call is never sent:

- the episode's call cap: the next call would exceed ``max_calls_per_episode``;
- the episode's dollar cap: ``max_usd_per_episode`` is already spent;
- the episode's seconds cap: ``max_seconds_per_episode`` is already spent, only
  when ``enforce_seconds`` is on (the live modes turn it on; backtests use the
  call budget alone);
- the run's dollar cap: ``max_usd_per_run`` is reached.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from pmfa.config import BudgetConfig
from pmfa.errors import BudgetExhausted, RunBudgetExhausted
from pmfa.records.episode import Usage


def add_usage(a: Usage, b: Usage) -> Usage:
    """The sum of two usages."""
    return Usage(
        calls=a.calls + b.calls,
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cost_usd=a.cost_usd + b.cost_usd,
        seconds=a.seconds + b.seconds,
    )


class Meter:
    """Counts calls, tokens, dollars, and seconds per episode and for the whole run. Thread-safe."""

    def __init__(self, budget: BudgetConfig, *, enforce_seconds: bool = False) -> None:
        self.budget = budget
        self.enforce_seconds = enforce_seconds
        self._episodes: dict[str, Usage] = {}
        self._run = Usage()
        self._lock = threading.Lock()

    def check(self, episode_id: str) -> None:
        """Raise instead of letting the next call go out when a cap is reached."""
        with self._lock:
            run = self._run
            episode = self._episodes.get(episode_id, Usage())
        b = self.budget
        if run.cost_usd >= b.max_usd_per_run:
            raise RunBudgetExhausted(
                f"run dollar cap reached: ${run.cost_usd} spent of ${b.max_usd_per_run}"
            )
        if episode.calls + 1 > b.max_calls_per_episode:
            raise BudgetExhausted(
                f"episode {episode_id}: call cap reached ({episode.calls} of "
                f"{b.max_calls_per_episode})"
            )
        if episode.cost_usd >= b.max_usd_per_episode:
            raise BudgetExhausted(
                f"episode {episode_id}: dollar cap reached (${episode.cost_usd} of "
                f"${b.max_usd_per_episode})"
            )
        if self.enforce_seconds and episode.seconds >= b.max_seconds_per_episode:
            raise BudgetExhausted(
                f"episode {episode_id}: seconds cap reached ({episode.seconds:.1f}s of "
                f"{b.max_seconds_per_episode:.1f}s)"
            )

    def add(self, episode_id: str, episode_delta: Usage, run_delta: Usage | None = None) -> None:
        """Charge a call to the episode and to the run (the run gets ``episode_delta`` unless told otherwise)."""
        with self._lock:
            current = self._episodes.get(episode_id, Usage())
            self._episodes[episode_id] = add_usage(current, episode_delta)
            self._run = add_usage(self._run, episode_delta if run_delta is None else run_delta)

    def episode(self, episode_id: str) -> Usage:
        with self._lock:
            return self._episodes.get(episode_id, Usage())

    def run(self) -> Usage:
        with self._lock:
            return self._run


def usage_of(
    *,
    calls: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal | None = None,
    seconds: float = 0.0,
) -> Usage:
    """A ``Usage`` delta for one call."""
    return Usage(
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=Decimal("0") if cost_usd is None else cost_usd,
        seconds=seconds,
    )


__all__ = ["Meter", "add_usage", "usage_of"]
