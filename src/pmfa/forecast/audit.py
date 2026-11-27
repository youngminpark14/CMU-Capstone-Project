"""The pruning audit: did the search stop expanding the path that happened?

Once an event resolves, each scenario of the forecast gets a posterior weight
``path mass x its probability of the realized outcome``, normalised over the
forecast's scenarios. The audit reports how much of that posterior sits on
frozen scenarios, and whether the single most likely scenario given the outcome
was a frozen one.
"""

from __future__ import annotations

from pmfa.records.base import Record
from pmfa.records.forecast import NodeStatus, OutcomeSpace, TreeNode

from .aggregate import counted_nodes


class PruningAudit(Record):
    event_ticker: str
    realized_outcome_id: str
    frozen_posterior_mass: float
    """Share of the posterior (given the realized outcome) on frozen scenarios, in [0, 1]."""
    most_likely_node_id: str | None
    most_likely_was_frozen: bool
    n_scenarios: int
    n_frozen: int


def pruning_audit(
    nodes: list[TreeNode], space: OutcomeSpace, realized_outcome_id: str
) -> PruningAudit:
    """Audit one episode's tree against the outcome that happened."""
    if realized_outcome_id not in space.outcome_ids():
        raise ValueError(f"{realized_outcome_id!r} is not an outcome of {space.event_ticker}")
    members = counted_nodes(nodes)
    weights = {
        n.node_id: (n.path_mass or 0.0)
        * (n.outcome_probabilities or {}).get(realized_outcome_id, 0.0)
        for n in members
    }
    total = sum(weights.values())
    frozen = {n.node_id for n in members if n.status is NodeStatus.FROZEN}
    if total <= 0.0:
        return PruningAudit(
            event_ticker=space.event_ticker,
            realized_outcome_id=realized_outcome_id,
            frozen_posterior_mass=0.0,
            most_likely_node_id=None,
            most_likely_was_frozen=False,
            n_scenarios=len(members),
            n_frozen=len(frozen),
        )
    best = min(weights, key=lambda nid: (-weights[nid], nid))
    return PruningAudit(
        event_ticker=space.event_ticker,
        realized_outcome_id=realized_outcome_id,
        frozen_posterior_mass=sum(w for nid, w in weights.items() if nid in frozen) / total,
        most_likely_node_id=best,
        most_likely_was_frozen=best in frozen,
        n_scenarios=len(members),
        n_frozen=len(frozen),
    )
