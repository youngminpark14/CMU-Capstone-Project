"""The scenario tree: building nodes from generator thoughts, and validating critic scores.

The critic's answer is checked in code before it touches the tree:

- exactly one score per scenario (otherwise ``ModelOutputError``);
- a rejected verdict, an unknown option id, or a missing distribution makes the
  scenario rejected (the latter two with reason ``invalid distribution``);
- categorical option probabilities are clipped at 0 and normalised to sum to 1;
  ladder quantiles go through ``quantiles.bin_probabilities``;
- sibling probabilities are clipped to [0, 1] and, if they sum to more than 1,
  scaled down to sum to 1. The remainder is unexplored mass.
"""

from __future__ import annotations

import math

from pmfa.errors import ModelOutputError
from pmfa.records.base import stable_id
from pmfa.records.evidence import Passage
from pmfa.records.forecast import NodeStatus, OutcomeSpace, TreeNode
from pmfa.records.roles import (
    PassageBrief,
    PathStep,
    RejectionBrief,
    ScenarioBrief,
    ScenarioScore,
    ScenarioThought,
    ScheduledEventBrief,
    ScoreOutput,
)

from .quantiles import bin_probabilities

INVALID_DISTRIBUTION = "invalid distribution"


def make_node(
    *,
    episode_id: str,
    parent: TreeNode | None,
    level: ScheduledEventBrief,
    depth: int,
    index: int,
    thought: ScenarioThought,
) -> TreeNode:
    parent_id = parent.node_id if parent is not None else None
    return TreeNode(
        node_id=stable_id("node", episode_id, parent_id or "root", level.scheduled_event_id, index),
        episode_id=episode_id,
        parent_id=parent_id,
        depth=depth,
        scheduled_event_id=level.scheduled_event_id,
        claim=thought.claim,
        outcome_ids=list(thought.outcome_ids),
        value_low=thought.value_low,
        value_high=thought.value_high,
        passage_ids=list(dict.fromkeys(thought.passage_ids)),
        rationale=thought.rationale,
    )


def path_steps(
    parent: TreeNode | None, by_id: dict[str, TreeNode], level_titles: dict[str, str]
) -> list[PathStep]:
    """The outcomes already assumed above a new level, root first."""
    chain: list[TreeNode] = []
    node = parent
    while node is not None:
        chain.append(node)
        node = by_id[node.parent_id] if node.parent_id is not None else None
    return [
        PathStep(
            scheduled_event_title=level_titles.get(n.scheduled_event_id, n.scheduled_event_id),
            claim=n.claim,
        )
        for n in reversed(chain)
    ]


def scenario_brief(node: TreeNode) -> ScenarioBrief:
    """What the critic sees of a scenario: never the generator's rationale."""
    return ScenarioBrief(
        scenario_id=node.node_id,
        claim=node.claim,
        outcome_ids=list(node.outcome_ids),
        value_low=node.value_low,
        value_high=node.value_high,
        passage_ids=list(node.passage_ids),
    )


def passage_brief(passage: Passage) -> PassageBrief:
    return PassageBrief(
        passage_id=passage.passage_id,
        source=passage.source,
        title=passage.title,
        url=passage.url,
        published_at=passage.published_at,
        text=passage.text,
    )


def _finite(value: float) -> float:
    return value if math.isfinite(value) else 0.0


def distribution(score: ScenarioScore, space: OutcomeSpace) -> dict[str, float] | None:
    """The scenario's outcome distribution after code conversion, or ``None`` if invalid."""
    if space.kind == "ladder":
        if score.quantiles is None:
            return None
        return bin_probabilities(space, score.quantiles)
    valid = set(space.outcome_ids())
    given = [op.outcome_id for op in score.option_probabilities]
    if not given or len(set(given)) != len(given) or not set(given) <= valid:
        return None
    raw = {op.outcome_id: max(0.0, _finite(op.probability)) for op in score.option_probabilities}
    total = sum(raw.values())
    if total <= 0.0:
        return None
    return {oid: raw.get(oid, 0.0) / total for oid in space.outcome_ids()}


def check_one_score_each(siblings: list[TreeNode], output: ScoreOutput) -> None:
    ids = [n.node_id for n in siblings]
    got = [s.scenario_id for s in output.scores]
    if sorted(got) != sorted(ids):
        raise ModelOutputError(
            f"the critic returned scores for {got}, expected exactly one for each of {ids}"
        )


def apply_scores(
    siblings: list[TreeNode],
    output: ScoreOutput,
    space: OutcomeSpace,
    parent_mass: float,
) -> tuple[list[TreeNode], list[RejectionBrief]]:
    """Sibling scenarios with the critic's validated scores, and the rejections it made."""
    check_one_score_each(siblings, output)
    scores = {s.scenario_id: s for s in output.scores}
    rejections: list[RejectionBrief] = []
    accepted: dict[str, tuple[float, dict[str, float]]] = {}
    updated: dict[str, TreeNode] = {}
    for node in siblings:
        score = scores[node.node_id]
        reason = ""
        dist: dict[str, float] | None = None
        if score.verdict == "rejected":
            reason = score.rejection_reason.strip() or "rejected by the critic"
        else:
            dist = distribution(score, space)
            if dist is None:
                reason = INVALID_DISTRIBUTION
        if dist is None:
            updated[node.node_id] = node.model_copy(
                update={
                    "status": NodeStatus.REJECTED,
                    "reason": reason,
                    "cond_probability": 0.0,
                    "path_mass": 0.0,
                    "critic_rationale": score.rationale,
                }
            )
            rejections.append(RejectionBrief(claim=node.claim, reason=reason))
            continue
        accepted[node.node_id] = (min(1.0, max(0.0, _finite(score.probability))), dist)

    total = sum(p for p, _ in accepted.values())
    scale = 1.0 / total if total > 1.0 else 1.0
    for node in siblings:
        if node.node_id not in accepted:
            continue
        p, dist = accepted[node.node_id]
        p *= scale
        score = scores[node.node_id]
        updated[node.node_id] = node.model_copy(
            update={
                "status": NodeStatus.SCORED,
                "reason": "",
                "cond_probability": p,
                "path_mass": parent_mass * p,
                "outcome_probabilities": dist,
                "quantiles": score.quantiles if space.kind == "ladder" else None,
                "critic_rationale": score.rationale,
            }
        )
    return [updated[n.node_id] for n in siblings], rejections
