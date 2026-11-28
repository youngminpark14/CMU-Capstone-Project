"""Aggregation: from the scored tree to one forecast for every market of the event.

The forecast is made of every scored scenario that has no scored children:
frozen ones, leaves, and expanded ones whose expansion produced nothing. Frozen
scenarios stay in with their probability and last outcome distribution;
dropping them would renormalise the forecast toward the likeliest path.

A market's probability is ``sum(path mass * scenario probability for the
market) / total mass``. Coverage is that total mass; whatever the critic left
unassigned among siblings is unexplored mass and lowers it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pmfa.records.base import stable_id
from pmfa.records.evidence import Passage
from pmfa.records.forecast import Forecast, NodeStatus, OutcomeSpace, SupportingPath, TreeNode

from .quantiles import market_probabilities

SCORED_STATUSES = frozenset(
    {NodeStatus.SCORED, NodeStatus.EXPANDED, NodeStatus.FROZEN, NodeStatus.LEAF}
)
"""Statuses of scenarios the critic scored and the search kept."""


def confidence(
    coverage: float, n_origins: int, *, full_origins: int, budget_exhausted: bool
) -> float:
    """Forecast confidence: coverage x min(1, independent origins / full_origins), halved when
    the budget ran out. It can only reduce size."""
    origin_factor = min(1.0, n_origins / full_origins) if full_origins > 0 else 1.0
    value = max(0.0, min(1.0, coverage)) * origin_factor
    return value / 2.0 if budget_exhausted else value


def counted_nodes(
    nodes: Iterable[TreeNode], exclude: frozenset[str] = frozenset()
) -> list[TreeNode]:
    """The scenarios that make up the forecast, minus ``exclude`` (and nothing else changes)."""
    nodes = list(nodes)
    scored = [n for n in nodes if n.status in SCORED_STATUSES]
    has_scored_child = {n.parent_id for n in scored if n.parent_id is not None}
    return [
        n
        for n in scored
        if n.node_id not in has_scored_child
        and n.node_id not in exclude
        and n.outcome_probabilities
    ]


def subtree_ids(nodes: Iterable[TreeNode], root_id: str) -> frozenset[str]:
    """``root_id`` and every node below it."""
    nodes = list(nodes)
    ids = {root_id}
    grew = True
    while grew:
        grew = False
        for n in nodes:
            if n.parent_id in ids and n.node_id not in ids:
                ids.add(n.node_id)
                grew = True
    return frozenset(ids)


def path_to(node: TreeNode, by_id: dict[str, TreeNode]) -> list[TreeNode]:
    """Nodes from the first level down to ``node``."""
    path = [node]
    while path[-1].parent_id is not None:
        path.append(by_id[path[-1].parent_id])
    return path[::-1]


def aggregate(
    nodes: list[TreeNode],
    space: OutcomeSpace,
    *,
    episode_id: str,
    as_of: datetime,
    passages: dict[str, Passage],
    counted_origins: set[str],
    full_origins: int,
    budget_exhausted: bool = False,
    fast_path: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> Forecast | None:
    """The forecast from the tree, or ``None`` when no scenario carries any mass."""
    members = counted_nodes(nodes, exclude)
    total = sum(n.path_mass or 0.0 for n in members)
    if total <= 0.0:
        return None
    outcome_probabilities = dict.fromkeys(space.outcome_ids(), 0.0)
    for n in members:
        mass = n.path_mass or 0.0
        for oid, p in (n.outcome_probabilities or {}).items():
            outcome_probabilities[oid] = outcome_probabilities.get(oid, 0.0) + mass * p
    outcome_probabilities = {oid: p / total for oid, p in outcome_probabilities.items()}

    by_id = {n.node_id: n for n in nodes}
    paths: list[SupportingPath] = []
    cited: list[str] = []
    for n in sorted(members, key=lambda m: (-(m.path_mass or 0.0), m.node_id)):
        steps = path_to(n, by_id)
        paths.append(
            SupportingPath(
                node_ids=[s.node_id for s in steps],
                claims=[s.claim for s in steps],
                mass=n.path_mass or 0.0,
                status=n.status,
            )
        )
        for s in steps:
            cited.extend(pid for pid in s.passage_ids if pid not in cited)
    origins = {
        passages[pid].origin_id
        for pid in cited
        if pid in passages and passages[pid].origin_id in counted_origins
    }
    coverage = min(1.0, total)
    return Forecast(
        forecast_id=stable_id("fc", episode_id, space.event_ticker, as_of.isoformat()),
        episode_id=episode_id,
        event_ticker=space.event_ticker,
        as_of=as_of,
        outcome_probabilities=outcome_probabilities,
        market_probabilities=market_probabilities(space, outcome_probabilities),
        coverage=coverage,
        confidence=confidence(
            coverage, len(origins), full_origins=full_origins, budget_exhausted=budget_exhausted
        ),
        n_independent_origins=len(origins),
        budget_exhausted=budget_exhausted,
        depth_reached=max((n.depth for n in nodes if n.status in SCORED_STATUSES), default=0),
        fast_path=fast_path,
        paths=paths,
        passage_ids=cited,
    )
