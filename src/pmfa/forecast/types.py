"""Records and callback types of the forecast engine.

These records belong to the forecast module: the episode controller builds a
``ForecastRequest``, checkpoints a ``SearchState`` between beam steps, and reads
a ``ForecastResult``. They are serialisable, so a state survives
``model_dump_json`` / ``model_validate_json`` between graph nodes.
"""

from __future__ import annotations

from collections.abc import Callable

from pmfa.config import SeriesConfig
from pmfa.records.base import Dollars, Record, UtcTime
from pmfa.records.decision import SizingSummary
from pmfa.records.episode import Exit
from pmfa.records.evidence import Origin, Passage
from pmfa.records.forecast import Forecast, OutcomeSpace, TreeNode
from pmfa.records.market import Event
from pmfa.records.roles import LedgerExample, RejectionBrief, ScheduledEventBrief


class ForecastRequest(Record):
    """Everything one search needs, fixed for the whole episode."""

    episode_id: str
    event: Event
    space: OutcomeSpace
    series: SeriesConfig
    levels: list[ScheduledEventBrief]
    """Root first; the last level is always the resolution event (see ``levels.levels``)."""
    passages: list[Passage]
    """The passages supplied to the episode. Scenarios may cite only these (and follow-ups)."""
    origins: list[Origin]
    """The counted origins behind ``passages``."""
    rejections: list[RejectionBrief] = []
    """Scenarios the critic rejected on this event in earlier episodes."""
    ledger_examples: list[LedgerExample] = []
    as_of: UtcTime
    bankroll: Dollars
    fast_path: bool = False


class EvidenceBatch(Record):
    """What the ``more_evidence`` callback returns for one critic evidence request."""

    passages: list[Passage] = []
    origins: list[Origin] = []


class SearchState(Record):
    """The beam search between steps: the request, the tree so far, and counters."""

    request: ForecastRequest
    levels: list[ScheduledEventBrief]
    """The levels this search walks (one level on the fast path)."""
    nodes: list[TreeNode] = []
    level_index: int = 0
    """The next level to generate, 0-based. ``TreeNode.depth`` is ``level_index + 1``."""
    extra_passages: list[Passage] = []
    """Passages added by evidence follow-ups."""
    extra_origins: list[Origin] = []
    rejections: list[RejectionBrief] = []
    """Request rejections plus the ones made in this search, passed to every GENERATE call."""
    evidence_followups: int = 0
    generator_calls: int = 0
    critic_calls: int = 0
    protected_node_ids: list[str] = []
    """Scenarios that took the protected slot, shallowest first."""
    budget_exhausted: bool = False
    exit: Exit | None = None
    exit_detail: str = ""


class ForecastResult(Record):
    """The Forecast, or an Exit (``rules_mismatch`` or ``low_coverage``), plus every node."""

    forecast: Forecast | None = None
    exit: Exit | None = None
    detail: str = ""
    nodes: list[TreeNode] = []
    budget_exhausted: bool = False
    generator_calls: int = 0
    critic_calls: int = 0


SizeFn = Callable[[Forecast], SizingSummary]
"""What sizing would do for a forecast. The controller closes over prices, holdings and bankroll."""

MoreEvidence = Callable[[str], EvidenceBatch]
"""Run one critic evidence request (a search query) as of the episode's as-of time."""

OnNode = Callable[[TreeNode], None]
"""Called with every node that was created or changed (insert-or-replace by ``node_id``)."""

OnRejection = Callable[[RejectionBrief], None]
"""Called with every scenario the critic rejected, so later episodes do not repeat it."""

IsTradable = Callable[[str], bool]
"""Whether a market ticker can be traded (open, not blocklisted). Used by the protected slot."""
