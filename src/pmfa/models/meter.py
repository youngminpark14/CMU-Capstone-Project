"""Per-episode and per-run meters for model calls, with the hard caps from ``config.budget``.

The meter is checked before every call, so a capped call is never sent:

- the episode's call cap: the next call would exceed ``max_calls_per_episode``;
- the episode's dollar cap: ``max_usd_per_episode`` is already spent, or would be
  passed if the next call cost its worst case;
- the episode's seconds cap: ``max_seconds_per_episode`` is already spent, only
  when ``enforce_seconds`` is on (the live modes turn it on; backtests use the
  call budget alone);
- the run's dollar cap: ``max_usd_per_run`` is reached, or would be passed if the
  next call cost its worst case.

A call's cost is known only after it returns, so the caller passes the call's
worst case (``worst_case_usd``): the gateway prices an upper estimate of the
prompt's input tokens plus the full ``max_tokens`` of output on the assigned
model. The dollar caps are then ceilings, as far as the input-token estimate is
an upper bound. An answer that costs nothing new (cached, cassette, scripted) is
checked with no worst case, exactly as before. The seconds cap is held the same
way: when it is enforced, the gateway gives each call a timeout no longer than
:meth:`Meter.seconds_left`. Seconds are model-call seconds only.
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

    def check(self, episode_id: str, worst_case_usd: Decimal = Decimal("0")) -> None:
        """Raise instead of letting the next call go out when a cap is reached.

        ``worst_case_usd`` is the most the next call can cost; a call that could take
        either dollar cap past its limit is refused before it is sent.
        """
        with self._lock:
            run = self._run
            episode = self._episodes.get(episode_id, Usage())
        b = self.budget
        worst = max(Decimal("0"), worst_case_usd)
        could = f"; the next call could cost up to ${worst}" if worst else ""
        if run.cost_usd >= b.max_usd_per_run or run.cost_usd + worst > b.max_usd_per_run:
            raise RunBudgetExhausted(
                f"run dollar cap reached: ${run.cost_usd} spent of ${b.max_usd_per_run}{could}"
            )
        if episode.calls + 1 > b.max_calls_per_episode:
            raise BudgetExhausted(
                f"episode {episode_id}: call cap reached ({episode.calls} of "
                f"{b.max_calls_per_episode})"
            )
        if (
            episode.cost_usd >= b.max_usd_per_episode
            or episode.cost_usd + worst > b.max_usd_per_episode
        ):
            raise BudgetExhausted(
                f"episode {episode_id}: dollar cap reached (${episode.cost_usd} of "
                f"${b.max_usd_per_episode}{could})"
            )
        if self.enforce_seconds and episode.seconds >= b.max_seconds_per_episode:
            raise BudgetExhausted(
                f"episode {episode_id}: seconds cap reached ({episode.seconds:.1f}s of "
                f"{b.max_seconds_per_episode:.1f}s)"
            )

    def seconds_left(self, episode_id: str) -> float | None:
        """Seconds the episode may still spend on model calls, or None when that cap is off."""
        if not self.enforce_seconds:
            return None
        with self._lock:
            spent = self._episodes.get(episode_id, Usage()).seconds
        return max(0.0, self.budget.max_seconds_per_episode - spent)

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
