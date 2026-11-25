"""Forecast engine: outcome spaces, the scenario tree, checks, beam search, the protected
slot, and aggregation.

It turns an event, its evidence and a ``Models`` port into a ``Forecast`` and does
no I/O of its own. Entry points:

- ``build_outcome_space``, ``event_brief`` (``outcomes``);
- ``levels``, ``is_fast_path`` (``levels``);
- ``ForecastEngine`` with ``run`` / ``start`` / ``step`` / ``done`` / ``finish`` (``engine``);
- ``ForecastRequest``, ``SearchState``, ``ForecastResult``, ``EvidenceBatch`` (``types``);
- ``confidence`` (``aggregate``), ``pruning_audit`` (``audit``).
"""

from __future__ import annotations

from .aggregate import confidence
from .audit import PruningAudit, pruning_audit
from .engine import ForecastEngine
from .levels import is_fast_path, levels, resolution_level_id
from .outcomes import (
    OutcomeSpaceError,
    build_outcome_space,
    event_brief,
    ladder_outcome_for_value,
    outcome_from_results,
)
from .quantiles import QuantileCdf, bin_probabilities, market_probabilities
from .types import EvidenceBatch, ForecastRequest, ForecastResult, SearchState

__all__ = [
    "EvidenceBatch",
    "ForecastEngine",
    "ForecastRequest",
    "ForecastResult",
    "OutcomeSpaceError",
    "PruningAudit",
    "QuantileCdf",
    "SearchState",
    "bin_probabilities",
    "build_outcome_space",
    "confidence",
    "event_brief",
    "is_fast_path",
    "ladder_outcome_for_value",
    "levels",
    "market_probabilities",
    "outcome_from_results",
    "pruning_audit",
    "resolution_level_id",
]
