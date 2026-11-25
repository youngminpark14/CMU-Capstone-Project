"""The dry-run estimate: model calls and dollars per system, with its assumptions stated.

Nothing here calls a model. Calls are counted from the schedule and the tree
the configuration allows:

- each one-call baseline makes one call per scoring point;
- a scheduled pipeline episode makes two Researcher calls (plan, select), one
  generator and one critic call per expanded parent (one parent at the first
  level, then the beam plus the protected slot at each further level, the
  depth being the calendar entries between the as-of time and the close plus
  the resolution, as the engine computes it), plus up to
  ``forecast.max_evidence_followups`` evidence follow-ups of one Researcher call
  each and one critic re-score per scoring call that had follow-ups, priced on
  the slot of its level (the depth-1 critic at the first level), capped by
  ``budget.max_calls_per_episode`` (the dearest calls kept);
- a news-triggered pipeline episode adds one Monitor call and, since the event
  it will match is unknown, is bounded with a full-depth tree.

The maximum counts no repair retries. The gateway gives an unusable answer one
metered retry, so an episode could make up to twice the counted calls (within
its call cap); the plan says so next to the maximum, and the run's dollar cap,
which the gateway checks at each call's worst case, is the real ceiling.

The floor is what a run costs if every episode stops after its first call.
A call's price is the mean recorded cost of answers to the same task on the
same model in the response cache when there are any; otherwise the token
assumptions below times the catalog price. Baseline calls always use the
assumptions: the cache does not record whether an answer was given passages,
so a recorded mean would blend the prompt-only and the with-passages calls.
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from pmfa.config import Config
from pmfa.evaluation.baselines import MARKET, PIPELINE, PROMPT_ONLY
from pmfa.evaluation.manifest import SystemPlan
from pmfa.evaluation.schedule import Schedule
from pmfa.forecast import levels
from pmfa.models.pricing import cost
from pmfa.records.market import Event
from pmfa.records.roles import ModelSlot, RoleTask
from pmfa.world.evidence.calendar import ReleaseCalendar

TOKEN_ASSUMPTIONS: dict[str, tuple[int, int]] = {
    "match": (3000, 600),
    "plan_queries": (1500, 800),
    "select_passages": (4500, 1200),
    "generate": (5000, 2500),
    "score": (6000, 6000),
    "baseline": (1500, 6000),
    "baseline_with_passages": (4500, 6000),
}
"""Assumed (input, output) tokens per call when the cache has no recorded cost.

Output includes thinking at the configured effort, which is why the critic and
the baseline (high effort) are assumed to write far more than their visible
answer. Input for the passage-reading calls assumes about eight 250-word passages.
"""


def recorded_costs(cache_dir: str | Path) -> dict[tuple[str, str], tuple[Decimal, int]]:
    """Mean recorded cost and count per (task, model) over the response cache's answers."""
    totals: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    folder = Path(cache_dir)
    if folder.is_dir():
        for file in sorted(folder.glob("*.json")):
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
                totals[(str(raw["task"]), str(raw["model"]))].append(Decimal(str(raw["cost_usd"])))
            except (OSError, ValueError, KeyError):
                continue
    return {key: (sum(v, Decimal("0")) / len(v), len(v)) for key, v in totals.items() if v}


class CallPricer:
    """The price of one call of a task on a slot: recorded when available, else assumed."""

    def __init__(self, config: Config, recorded: dict[tuple[str, str], tuple[Decimal, int]]):
        self.config = config
        self.recorded = recorded
        self.used: set[str] = set()

    def __call__(self, task: RoleTask, slot: ModelSlot, *, with_passages: bool = True) -> Decimal:
        model = self.config.models.slots[slot].model
        # The cache does not record whether a baseline answer had passages, so its
        # mean would blend prompt-only and with-passages answers; the baseline is
        # priced from the token assumptions instead.
        hit = None if task is RoleTask.BASELINE else self.recorded.get((task.value, model))
        if hit is not None:
            self.used.add(
                f"{task.value} on {model}: mean of {hit[1]} recorded answer(s), ${hit[0]:.4f}"
            )
            return hit[0]
        key = task.value
        if task is RoleTask.BASELINE and with_passages:
            key = "baseline_with_passages"
        tokens_in, tokens_out = TOKEN_ASSUMPTIONS[key]
        price = cost(model, tokens_in, tokens_out, catalog=self.config.models.catalog)
        self.used.add(
            f"{key} on {model}: assumed {tokens_in} input and {tokens_out} output tokens, "
            f"${price:.4f} per call"
        )
        return price


def _parents(depth: int, config: Config) -> int:
    return 1 + (depth - 1) * (config.forecast.beam_width + 1)


def _episode_bound(
    depth: int, config: Config, price: CallPricer, *, match: bool
) -> tuple[int, Decimal]:
    """The most calls and dollars one pipeline episode of this depth can spend."""
    parents = _parents(depth, config)
    followups = config.forecast.max_evidence_followups
    priced: list[Decimal] = []
    if match:
        priced.append(price(RoleTask.MATCH, ModelSlot.MONITOR))
    priced += [
        price(RoleTask.PLAN_QUERIES, ModelSlot.RESEARCHER),
        price(RoleTask.SELECT_PASSAGES, ModelSlot.RESEARCHER),
    ]
    priced += [price(RoleTask.GENERATE, ModelSlot.GENERATOR)] * parents
    scores = [price(RoleTask.SCORE, ModelSlot.CRITIC_DEPTH1)]
    scores += [price(RoleTask.SCORE, ModelSlot.CRITIC_DEEP)] * (parents - 1)
    priced += scores
    # Each follow-up is one Researcher call. The engine then re-scores a parent's
    # children once, on the critic slot of that parent's level, so there are at
    # most min(follow-ups, scoring calls) re-scores; the bound puts them on the
    # dearest scoring calls (the depth-1 critic's first).
    priced += [price(RoleTask.SELECT_PASSAGES, ModelSlot.RESEARCHER)] * followups
    priced += sorted(scores, reverse=True)[: min(followups, len(scores))]
    # Under the episode's call cap, the dearest calls are the ones kept.
    priced = sorted(priced, reverse=True)[: config.budget.max_calls_per_episode]
    return len(priced), sum(priced, Decimal("0"))


def plan_systems(
    schedule: Schedule,
    events: dict[str, Event],
    calendar: ReleaseCalendar,
    config: Config,
    *,
    systems: list[str],
    statuses: dict[str, str],
    score: bool,
    trade: bool,
    cache_dir: str | Path,
) -> tuple[list[SystemPlan], list[str]]:
    """Calls and dollars per system, and the assumptions behind the numbers."""
    price = CallPricer(config, recorded_costs(cache_dir))
    points = schedule.points if score else []
    plans: list[SystemPlan] = []
    for system in systems:
        status = statuses.get(system, "run")
        if system == MARKET:
            plans.append(SystemPlan(system=system, status=status, forecasts=len(points)))
            continue
        if system != PIPELINE:
            one = price(RoleTask.BASELINE, ModelSlot.BASELINE, with_passages=system != PROMPT_ONLY)
            plans.append(
                SystemPlan(
                    system=system,
                    status=status,
                    forecasts=len(points),
                    model_calls_max=len(points),
                    model_calls_min=len(points),
                    usd_max=one * len(points),
                    usd_min=one * len(points),
                    notes=["one baseline call per scoring point; a repair retry would add one"],
                )
            )
            continue
        calls_max = calls_min = 0
        usd_max = usd_min = Decimal("0")
        depths: dict[int, int] = defaultdict(int)
        for point in points:
            event = events[point.event_ticker]
            scheduled = calendar.between(point.as_of, event.close_time)
            depth = len(levels(event, scheduled, point.as_of, config))
            depths[depth] += 1
            n, usd = _episode_bound(depth, config, price, match=False)
            calls_max += n
            usd_max += usd
            calls_min += 1
            usd_min += price(RoleTask.PLAN_QUERIES, ModelSlot.RESEARCHER)
        triggers = schedule.triggers if trade else []
        for _ in triggers:
            n, usd = _episode_bound(config.forecast.max_depth, config, price, match=True)
            calls_max += n
            usd_max += usd
            calls_min += 1
            usd_min += price(RoleTask.MATCH, ModelSlot.MONITOR)
        notes = [
            "scheduled episodes by tree depth: "
            + (", ".join(f"depth {d}: {n}" for d, n in sorted(depths.items())) or "none"),
            f"{len(triggers)} news-triggered episode(s), each bounded with a full depth-"
            f"{config.forecast.max_depth} tree after one Monitor call",
            "the maximum assumes no repair retries: an unusable answer gets one retry, which "
            "could add up to as many calls again within each episode's call cap; the run's "
            f"dollar cap (${config.budget.max_usd_per_run}) is the real ceiling",
            "the floor assumes every episode stops after its first call (no candidate event, "
            "or no evidence)",
        ]
        plans.append(
            SystemPlan(
                system=system,
                status=status,
                forecasts=len(points) + len(triggers),
                model_calls_max=calls_max,
                model_calls_min=calls_min,
                usd_max=usd_max,
                usd_min=usd_min,
                notes=notes,
            )
        )
    assumptions = [
        "Prices are the catalog's dollars per million tokens (config.models.catalog).",
        *sorted(price.used),
        "Answers already in the response cache cost nothing on a rerun; the estimate does not "
        "subtract them.",
    ]
    return plans, assumptions


__all__ = ["TOKEN_ASSUMPTIONS", "CallPricer", "plan_systems", "recorded_costs"]
