"""The decision core: fees, ticks, sizing, the gate, risk state, and routing.

Pure functions with no I/O, no clock reads, and no model calls. Money is
decimal throughout. Callers gather the inputs (books, positions, equity,
configuration) and pass them in; these functions only decide.
"""

from __future__ import annotations

from pmfa.decision.confidence import origin_rule_met
from pmfa.decision.fees import (
    KNOWN_FEE_TYPES,
    UnknownFeeType,
    exact_fee,
    fee,
    fee_rate,
    per_contract_fee,
    round_up_cent,
    series_fee,
)
from pmfa.decision.gate import gate
from pmfa.decision.risk import risk_state, trading_day_start
from pmfa.decision.routing import route
from pmfa.decision.sizing import edge_bound, shrink, size_event, size_fn, summarize
from pmfa.decision.ticks import is_valid_tick, snap_down, snap_up, valid_ticks
from pmfa.decision.types import (
    Candidate,
    GateContext,
    RoutingContext,
    SizingDecision,
    SizingInputs,
)

__all__ = [
    "KNOWN_FEE_TYPES",
    "Candidate",
    "GateContext",
    "RoutingContext",
    "SizingDecision",
    "SizingInputs",
    "UnknownFeeType",
    "edge_bound",
    "exact_fee",
    "fee",
    "fee_rate",
    "gate",
    "is_valid_tick",
    "origin_rule_met",
    "per_contract_fee",
    "risk_state",
    "round_up_cent",
    "route",
    "series_fee",
    "shrink",
    "size_event",
    "size_fn",
    "snap_down",
    "snap_up",
    "summarize",
    "trading_day_start",
    "valid_ticks",
]
