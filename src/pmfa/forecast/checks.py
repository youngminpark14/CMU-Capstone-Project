"""Code checks that run on every scenario before the critic sees it.

A scenario that fails any check is deleted, and every check's result is kept on
the node so the trace shows why:

- **rules**: it bears on the event as written. At the resolution level it must
  assert outcomes that exist in the outcome space (categorical: a non-empty list
  of valid option ids; ladder: at least one finite bound, and ``value_low <
  value_high`` when both are given). At earlier levels it must not assert
  resolution outcomes at all.
- **calendar**: the level's scheduled event falls after the as-of time and no
  later than the event's close.
- **evidence**: it cites at least one passage, and every cited passage was
  supplied to the episode, was published at or before the as-of time, and
  belongs to a counted origin.
"""

from __future__ import annotations

import math
from datetime import datetime

from pmfa.records.evidence import Passage
from pmfa.records.forecast import CheckResult, OutcomeSpace, TreeNode
from pmfa.records.roles import ScheduledEventBrief


def rules_check(node: TreeNode, level: ScheduledEventBrief, space: OutcomeSpace) -> CheckResult:
    asserts_values = node.value_low is not None or node.value_high is not None
    if not level.is_resolution:
        if node.outcome_ids or asserts_values:
            return CheckResult(
                name="rules",
                passed=False,
                detail="asserts a resolution outcome before the resolution level",
            )
        return CheckResult(name="rules", passed=True)
    if space.kind == "categorical":
        if not node.outcome_ids:
            return CheckResult(name="rules", passed=False, detail="asserts no option")
        unknown = sorted(set(node.outcome_ids) - set(space.outcome_ids()))
        if unknown:
            return CheckResult(
                name="rules", passed=False, detail=f"unknown options: {', '.join(unknown)}"
            )
        return CheckResult(name="rules", passed=True)
    bounds = [b for b in (node.value_low, node.value_high) if b is not None]
    if not bounds or not all(math.isfinite(b) for b in bounds):
        return CheckResult(name="rules", passed=False, detail="asserts no finite bound")
    if (
        node.value_low is not None
        and node.value_high is not None
        and not node.value_low < node.value_high
    ):
        return CheckResult(name="rules", passed=False, detail="value_low is not below value_high")
    return CheckResult(name="rules", passed=True)


def calendar_check(level: ScheduledEventBrief, as_of: datetime, closes_at: datetime) -> CheckResult:
    if level.scheduled_at <= as_of:
        return CheckResult(
            name="calendar", passed=False, detail="the scheduled event is not after the as-of time"
        )
    if level.scheduled_at > closes_at:
        return CheckResult(
            name="calendar", passed=False, detail="the scheduled event is after the event's close"
        )
    return CheckResult(name="calendar", passed=True)


def evidence_check(
    node: TreeNode,
    passages: dict[str, Passage],
    counted_origins: set[str],
    as_of: datetime,
) -> CheckResult:
    if not node.passage_ids:
        return CheckResult(name="evidence", passed=False, detail="cites no passage")
    problems: list[str] = []
    for pid in node.passage_ids:
        passage = passages.get(pid)
        if passage is None:
            problems.append(f"{pid} was not supplied")
        elif passage.published_at > as_of:
            problems.append(f"{pid} was published after the as-of time")
        elif passage.origin_id not in counted_origins:
            problems.append(f"{pid} has no counted origin")
    if problems:
        return CheckResult(name="evidence", passed=False, detail="; ".join(problems))
    return CheckResult(name="evidence", passed=True)


def run_checks(
    node: TreeNode,
    level: ScheduledEventBrief,
    space: OutcomeSpace,
    passages: dict[str, Passage],
    counted_origins: set[str],
    as_of: datetime,
    closes_at: datetime,
) -> list[CheckResult]:
    """All three checks, in a fixed order. The node passes only if every one passes."""
    return [
        rules_check(node, level, space),
        calendar_check(level, as_of, closes_at),
        evidence_check(node, passages, counted_origins, as_of),
    ]
