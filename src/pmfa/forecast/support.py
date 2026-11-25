"""Evidence support and the beam's ordering rule.

**Evidence support** is a factor in [0, 1] that says how well a scenario's own
citations back it::

    support = min(1, sum over the independent origins it cites of weight * recency / 2)

with weight 2 for a primary origin and 1 otherwise, and recency
``0.5 ** (age in days / half-life)`` for the category's half-life. One fresh
primary origin, or two fresh independent secondary ones, give full support.

**Beam order.** Scenarios are ranked by ``path mass * evidence support``. The
order is built by repeatedly taking, among the candidates whose score is within
``tie_band`` of the best remaining score, the one with more independent origins,
then fresher evidence, then the higher score, then the lower node id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pmfa.records.evidence import Origin, Passage
from pmfa.records.forecast import TreeNode

PRIMARY_WEIGHT = 2.0
SECONDARY_WEIGHT = 1.0


@dataclass(frozen=True)
class Support:
    value: float
    n_origins: int
    freshest: datetime | None


def evidence_support(
    passage_ids: list[str],
    passages: dict[str, Passage],
    origins: dict[str, Origin],
    as_of: datetime,
    half_life_days: float,
) -> Support:
    """Support from the independent counted origins behind the cited passages."""
    cited = {
        passages[pid].origin_id
        for pid in passage_ids
        if pid in passages and passages[pid].origin_id in origins
    }
    total = 0.0
    freshest: datetime | None = None
    for origin_id in sorted(cited):
        origin = origins[origin_id]
        age_days = max(0.0, (as_of - origin.published_at).total_seconds() / 86400.0)
        recency = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
        weight = PRIMARY_WEIGHT if origin.is_primary else SECONDARY_WEIGHT
        total += weight * recency
        if freshest is None or origin.published_at > freshest:
            freshest = origin.published_at
    return Support(value=min(1.0, total / 2.0), n_origins=len(cited), freshest=freshest)


def beam_score(node: TreeNode) -> float:
    return (node.path_mass or 0.0) * (node.evidence_support or 0.0)


_OLDEST = datetime.min.replace(tzinfo=UTC)
_EPS = 1e-12
"""Float slack so a score exactly ``tie_band`` below the best counts as within it."""


def beam_order(nodes: list[TreeNode], tie_band: float) -> list[TreeNode]:
    """Scenarios in the order the beam takes them (best first), with the tie rule."""
    remaining = list(nodes)
    ordered: list[TreeNode] = []
    while remaining:
        best = max(beam_score(n) for n in remaining)
        band = [n for n in remaining if beam_score(n) >= best - tie_band - _EPS]
        pick = min(
            band,
            key=lambda n: (
                -n.n_origins,
                -(n.freshest_evidence_at or _OLDEST).timestamp(),
                -beam_score(n),
                n.node_id,
            ),
        )
        ordered.append(pick)
        remaining.remove(pick)
    return ordered
