"""Forecast records: outcome spaces, scenario-tree nodes, and the forecast itself."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pmfa.records.base import Record, UtcTime
from pmfa.records.roles import Quantiles


class Outcome(Record):
    """One of the mutually exclusive outcomes of an event."""

    outcome_id: str
    label: str
    low: float | None = None
    """Ladder bins cover ``(low, high]`` of the resolving number; ``None`` is unbounded."""
    high: float | None = None


class OutcomeSpace(Record):
    """The mutually exclusive, exhaustive outcomes of an event, derived from its markets.

    Options for a categorical event; numeric bins between strikes for a ladder.
    Every market maps to the subset of outcomes on which it resolves YES.
    """

    event_ticker: str
    kind: Literal["categorical", "ladder"]
    outcomes: list[Outcome]
    market_outcomes: dict[str, list[str]]
    variable: str = ""
    unit: str = ""
    increment: float | None = None
    """Ladders: the step in which the resolving number is reported (0.1 for CPI)."""

    def outcome_ids(self) -> list[str]:
        return [o.outcome_id for o in self.outcomes]


class NodeStatus(StrEnum):
    PROPOSED = "proposed"
    DELETED = "deleted"  # failed a code check
    REJECTED = "rejected"  # rejected by the critic, with a reason
    SCORED = "scored"  # scored, not yet placed by the search
    EXPANDED = "expanded"  # in the beam or the protected slot; children were generated
    FROZEN = "frozen"  # outside the beam: not expanded further, still counted
    LEAF = "leaf"  # scored at the deepest level; counted


class CheckResult(Record):
    name: Literal["rules", "calendar", "evidence"]
    passed: bool
    detail: str = ""


class TreeNode(Record):
    """A scenario: a path of assumed outcomes for the scheduled events before resolution.

    The root is implicit (``parent_id`` is ``None`` for first-level nodes).
    """

    node_id: str
    episode_id: str
    parent_id: str | None
    depth: int
    """1 for first-level scenarios."""
    scheduled_event_id: str
    claim: str
    outcome_ids: list[str] = []
    value_low: float | None = None
    value_high: float | None = None
    passage_ids: list[str] = []
    rationale: str = ""
    """The generator's short rationale. The critic never sees it."""
    status: NodeStatus = NodeStatus.PROPOSED
    reason: str = ""
    """Why the node was deleted, rejected, frozen, or expanded (``beam`` or ``protected``)."""
    checks: list[CheckResult] = []
    cond_probability: float | None = None
    """Probability given the parent path, from the critic."""
    path_mass: float | None = None
    """Running probability: the product of ``cond_probability`` along the path."""
    outcome_probabilities: dict[str, float] | None = None
    """Distribution over the outcome space if this path holds (after code conversion)."""
    quantiles: Quantiles | None = None
    evidence_support: float | None = None
    n_origins: int = 0
    freshest_evidence_at: UtcTime | None = None
    critic_rationale: str = ""
    evidence_requested: bool = False


class SupportingPath(Record):
    node_ids: list[str]
    claims: list[str]
    mass: float
    status: NodeStatus


class Forecast(Record):
    """A probability for every market in the event, a confidence, and the supporting paths."""

    forecast_id: str
    episode_id: str
    event_ticker: str
    as_of: UtcTime
    system: str = "pipeline"
    """``pipeline``, or a baseline: ``prompt_only``, ``single_pass``, ``market``."""
    outcome_probabilities: dict[str, float]
    market_probabilities: dict[str, float]
    coverage: float
    """Total path mass of the scenarios that make up the forecast."""
    confidence: float
    n_independent_origins: int
    budget_exhausted: bool = False
    depth_reached: int = 0
    fast_path: bool = False
    protected_node_id: str | None = None
    protected_drove_size: bool = False
    paths: list[SupportingPath] = []
    passage_ids: list[str] = []
