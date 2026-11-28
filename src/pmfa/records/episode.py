"""Episode records: the trace, the named exit, the ledger, and operational state."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from pmfa.records.base import Dollars, Record, RunMode, Stage, UtcTime
from pmfa.records.decision import GateResult, OrderResult, Proposal, Review, Routing
from pmfa.records.evidence import Origin, Trigger
from pmfa.records.forecast import Forecast
from pmfa.records.roles import ModelSlot, RoleTask


class Exit(StrEnum):
    """How an episode ended. Every episode ends in exactly one exit."""

    NO_MATCH = "no_match"
    NO_EVIDENCE = "no_evidence"
    INSUFFICIENT_ORIGINS = "insufficient_origins"
    RULES_MISMATCH = "rules_mismatch"
    LOW_COVERAGE = "low_coverage"
    NO_EDGE = "no_edge"
    GATE_REJECTED = "gate_rejected"
    REJECTED_BY_HUMAN = "rejected_by_human"
    EXPIRED = "expired"
    HALTED = "halted"
    STALE_PRICE = "stale_price"
    EXECUTED = "executed"
    ERROR = "error"


class EpisodeStatus(StrEnum):
    RUNNING = "running"
    PENDING_REVIEW = "pending_review"
    DONE = "done"
    OUTCOME_UNKNOWN = "outcome_unknown"
    """Its order may stand on the exchange, unconfirmed: no exit yet; looked up again, never resent."""


class CallMeta(Record):
    """What one model call cost and where its answer came from."""

    task: RoleTask
    slot: ModelSlot
    role: str
    model: str
    served_model: str = ""
    """The model that answered, when a server-side fallback replaced the requested one."""
    effort: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    source: Literal["live", "cache", "cassette", "scripted"]
    repaired: bool = False
    """True when the first answer failed validation and the repair retry succeeded."""


class ModelResult(Record):
    output: Any
    """The validated output record for the task."""
    meta: CallMeta


def _zero(value: Any) -> bool:
    return not value


class Usage(Record):
    """Model calls, tokens, dollars and seconds charged to an episode or a run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    """Everything charged against the dollar caps, ``failed_cost_usd`` included."""
    seconds: float = 0.0
    failed_calls: int = Field(default=0, exclude_if=_zero)
    """Attempts that failed in the API client before reporting their usage."""
    failed_cost_usd: Decimal = Field(default=Decimal("0"), exclude_if=_zero)
    """The worst-case price charged for those attempts. The provider may have billed less or
    nothing. Both fields are left out of the JSON while zero, so a record written before
    they existed reads and writes the same."""


class TraceStep(Record):
    """One line of an episode's trace.

    Each pipeline step is logged as a thought (the role's short rationale), an
    action (what code executed), and an observation (what came back).
    """

    seq: int
    node: str
    """The pipeline step: ``match``, ``research``, ``forecast``, ``size``, ``gate``, ``route``, ``review``, ``execute``, ``reconcile``, ``record``."""
    kind: Literal["thought", "action", "observation"]
    summary: str
    role: str | None = None
    data: dict[str, Any] = {}
    """Input and output records as plain JSON, when the step has them."""
    call: CallMeta | None = None
    at: UtcTime


class EpisodeRecord(Record):
    """Everything one pass of the pipeline decided, for one trigger and one event."""

    episode_id: str
    mode: RunMode
    trigger: Trigger
    as_of: UtcTime
    event_ticker: str | None = None
    status: EpisodeStatus = EpisodeStatus.RUNNING
    exit: Exit | None = None
    exit_detail: str = ""
    passage_ids: list[str] = []
    origins: list[Origin] = []
    forecast: Forecast | None = None
    proposal: Proposal | None = None
    gate: GateResult | None = None
    routing: Routing | None = None
    review: Review | None = None
    orders: list[OrderResult] = []
    fill_tagged: bool = False
    """An approve-tier proposal was filled with no human present (backtests); reports separate these."""
    usage: Usage = Usage()
    trace: list[TraceStep] = []
    started_at: UtcTime
    finished_at: UtcTime | None = None


class LedgerEntry(Record):
    """One forecast for one market, with the price at forecast time and, later, the result."""

    entry_id: str
    episode_id: str
    mode: RunMode
    system: str = "pipeline"
    event_ticker: str
    market_ticker: str
    category: str
    question: str
    as_of: UtcTime
    probability: float
    """Forecast probability of YES, before shrinkage."""
    market_price: float | None
    """The YES mid at the as-of time."""
    outcome: bool | None = None
    resolved_at: UtcTime | None = None
    brier: float | None = None
    market_brier: float | None = None
    traded: bool = False
    tier: str = ""
    override: str = ""
    """The reviewer's decision and reason code, when a human changed or refused the trade."""


class Calibration(Record):
    category: str
    n_resolved: int
    calibration_error: float | None
    """Expected calibration error over resolved forecasts; ``None`` when there are none."""


class HaltState(Record):
    halted: bool
    reason: str = ""
    at: UtcTime | None = None


class RiskState(Record):
    """Bankroll and stop conditions, computed in code from fills and marks."""

    bankroll: Dollars
    day_start_equity: Dollars
    peak_equity: Dollars
    day_pnl: Dollars
    drawdown_fraction: Decimal
    loss_stop_tripped: bool
    drawdown_tripped: bool


class StageState(Record):
    stage: Stage
    reason: str = ""
    at: UtcTime | None = None
