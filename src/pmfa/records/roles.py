"""Records the LLM roles read and emit.

Roles are record-only: they receive one input record and return one output
record. They never call tools. Code executes every retrieval, check, and order.

Output records are sent to the model as a JSON schema (native structured
output), which constrains how they are written:

- every object is closed (``extra="forbid"`` gives ``additionalProperties: false``);
- no free-form dicts: use lists of small key/value records;
- no numeric or string constraints in the schema: ranges are stated in the field
  description and enforced by validators after parsing;
- a rationale is one or two sentences, never step-by-step reasoning.
"""

from __future__ import annotations

import itertools
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from pmfa.records.base import Record, UtcTime

# --------------------------------------------------------------------------- shared briefs


class OptionBrief(Record):
    outcome_id: str
    label: str


class EventBrief(Record):
    """What a role sees about an event. It never includes a market price."""

    event_ticker: str
    title: str
    category: str
    closes_at: UtcTime
    rules: str
    outcome_kind: Literal["categorical", "ladder"]
    options: list[OptionBrief] = []
    """Categorical events: the mutually exclusive options."""
    variable: str = ""
    """Ladder events: the number the event resolves on, e.g. "CPI-U 12-month percent change"."""
    unit: str = ""
    thresholds: list[float] = []
    """Ladder events: the strikes, ascending."""


class PassageBrief(Record):
    """Untrusted evidence text. Prompts pass it inside a delimited block."""

    passage_id: str
    source: str
    title: str
    url: str
    published_at: UtcTime
    text: str


class TriggerBrief(Record):
    kind: str
    headline: str
    excerpt: str
    source: str
    published_at: UtcTime


class ScheduledEventBrief(Record):
    scheduled_event_id: str
    title: str
    scheduled_at: UtcTime
    is_resolution: bool


class PathStep(Record):
    """One assumed outcome on the path from the root to a scenario."""

    scheduled_event_title: str
    claim: str


class RejectionBrief(Record):
    claim: str
    reason: str


class LedgerExample(Record):
    question: str
    forecast_probability: float
    market_price: float
    outcome: bool


# --------------------------------------------------------------------------- Monitor


class MatchInput(Record):
    trigger: TriggerBrief
    candidates: list[EventBrief]
    as_of: UtcTime


class MatchDecision(Record):
    matched: bool = Field(
        description="True if the trigger bears on at least one of the candidate events."
    )
    event_ticker: str = Field(
        description=(
            "Ticker of the candidate the trigger bears on most directly. "
            "Empty string when matched is false."
        )
    )
    rationale: str = Field(description="One or two sentences.")


# --------------------------------------------------------------------------- Researcher


class QueryPlanInput(Record):
    trigger: TriggerBrief
    event: EventBrief
    as_of: UtcTime


class QueryPlan(Record):
    query_for: str = Field(
        description="A search query for evidence that supports the trigger's implied direction."
    )
    query_against: str = Field(description="A search query for evidence against it.")
    rationale: str = Field(description="One or two sentences.")


class PassageSelectionInput(Record):
    event: EventBrief
    passages: list[PassageBrief]
    as_of: UtcTime


class DroppedPassage(Record):
    passage_id: str
    reason: str = Field(description="A few words on why the passage does not bear on the event.")


class PassageSelection(Record):
    keep_passage_ids: list[str] = Field(
        description="Ids of the passages that bear on the event as its rules are written."
    )
    dropped: list[DroppedPassage]
    rationale: str = Field(description="One or two sentences.")


# --------------------------------------------------------------------------- Generator


class GenerateInput(Record):
    event: EventBrief
    level: ScheduledEventBrief
    """The scheduled event this level of the tree is about."""
    path: list[PathStep]
    """Outcomes already assumed, root first. Empty at the first level."""
    passages: list[PassageBrief]
    rejected: list[RejectionBrief]
    """Scenarios the critic already rejected on this event. Do not repeat them."""
    max_thoughts: int
    as_of: UtcTime


class ScenarioThought(Record):
    """One claim about the level's scheduled event, with the passages that support it."""

    claim: str = Field(
        description="One sentence stating what happens at the level's scheduled event."
    )
    outcome_ids: list[str] = Field(
        description=(
            "Only when the level is the resolution of a categorical event: the option ids "
            "this claim asserts. Otherwise an empty list."
        )
    )
    value_low: float | None = Field(
        description="Only when the level is the resolution of a ladder event: lower end of the asserted range. Otherwise null."
    )
    value_high: float | None = Field(
        description="Only when the level is the resolution of a ladder event: upper end of the asserted range. Otherwise null."
    )
    passage_ids: list[str] = Field(description="Ids of the passages that support the claim.")
    rationale: str = Field(description="One or two sentences.")


class GenerateOutput(Record):
    thoughts: list[ScenarioThought] = Field(
        description="Up to max_thoughts alternative scenarios, mutually exclusive."
    )


# --------------------------------------------------------------------------- Critic


class ScenarioBrief(Record):
    """A scenario as the critic sees it: the claim and its citations, never the generator's rationale."""

    scenario_id: str
    claim: str
    outcome_ids: list[str] = []
    value_low: float | None = None
    value_high: float | None = None
    passage_ids: list[str] = []


class ScoreInput(Record):
    event: EventBrief
    level: ScheduledEventBrief
    path: list[PathStep]
    scenarios: list[ScenarioBrief]
    """Sibling scenarios under one parent, scored together."""
    passages: list[PassageBrief]
    ledger_examples: list[LedgerExample] = []
    as_of: UtcTime


class OptionProbability(Record):
    outcome_id: str
    probability: float = Field(description="Between 0 and 1.")


class Quantiles(Record):
    """Five quantiles of the number a ladder event resolves on."""

    q05: float
    q25: float
    q50: float
    q75: float
    q95: float

    @model_validator(mode="after")
    def _ascending(self) -> Quantiles:
        values = [self.q05, self.q25, self.q50, self.q75, self.q95]
        if any(b < a for a, b in itertools.pairwise(values)):
            raise ValueError("quantiles must be non-decreasing")
        return self


class ScenarioScore(Record):
    scenario_id: str
    verdict: Literal["scored", "rejected"]
    probability: float = Field(
        description=(
            "Probability of this scenario given its parent path, between 0 and 1. Sibling "
            "probabilities sum to at most 1; leave the rest for outcomes no sibling covers. "
            "0 when rejected."
        )
    )
    option_probabilities: list[OptionProbability] = Field(
        description=(
            "Categorical events: the probability of each option if this path holds, summing "
            "to 1. Empty for ladder events and for rejected scenarios."
        )
    )
    quantiles: Quantiles | None = Field(
        description=(
            "Ladder events: the 5th, 25th, 50th, 75th and 95th percentiles of the resolving "
            "number if this path holds. Null for categorical events and rejected scenarios."
        )
    )
    rejection_reason: str = Field(
        description="Why the scenario is rejected. Empty string when scored."
    )
    evidence_request: str = Field(
        description="A search query if more evidence would change this score, else an empty string."
    )
    rationale: str = Field(description="One or two sentences.")


class ScoreOutput(Record):
    scores: list[ScenarioScore] = Field(
        description="One entry per scenario in the input, in the same order."
    )


# --------------------------------------------------------------------------- Baselines


class BaselineInput(Record):
    """One-call forecast used by the evaluation: prompt-only (no passages) or single-pass."""

    event: EventBrief
    passages: list[PassageBrief] = []
    as_of: UtcTime


class BaselineForecast(Record):
    option_probabilities: list[OptionProbability] = Field(
        description="Categorical events: the probability of each option, summing to 1. Empty for ladder events."
    )
    quantiles: Quantiles | None = Field(
        description="Ladder events: five percentiles of the resolving number. Null for categorical events."
    )
    rationale: str = Field(description="One or two sentences.")


# --------------------------------------------------------------------------- tasks and slots


class RoleTask(StrEnum):
    """A kind of model call: one prompt, one input record type, one output record type."""

    MATCH = "match"
    PLAN_QUERIES = "plan_queries"
    SELECT_PASSAGES = "select_passages"
    GENERATE = "generate"
    SCORE = "score"
    BASELINE = "baseline"


class ModelSlot(StrEnum):
    """A named model-and-effort assignment in the configuration."""

    MONITOR = "monitor"
    RESEARCHER = "researcher"
    GENERATOR = "generator"
    CRITIC_DEPTH1 = "critic_depth1"
    CRITIC_DEEP = "critic_deep"
    FAST_PATH = "fast_path"
    BASELINE = "baseline"


class TaskSpec(Record):
    role: Literal["Monitor", "Researcher", "Generator", "Critic", "Baseline"]
    input_type: type[Record]
    output_type: type[Record]
    default_slot: ModelSlot


TASKS: dict[RoleTask, TaskSpec] = {
    RoleTask.MATCH: TaskSpec(
        role="Monitor",
        input_type=MatchInput,
        output_type=MatchDecision,
        default_slot=ModelSlot.MONITOR,
    ),
    RoleTask.PLAN_QUERIES: TaskSpec(
        role="Researcher",
        input_type=QueryPlanInput,
        output_type=QueryPlan,
        default_slot=ModelSlot.RESEARCHER,
    ),
    RoleTask.SELECT_PASSAGES: TaskSpec(
        role="Researcher",
        input_type=PassageSelectionInput,
        output_type=PassageSelection,
        default_slot=ModelSlot.RESEARCHER,
    ),
    RoleTask.GENERATE: TaskSpec(
        role="Generator",
        input_type=GenerateInput,
        output_type=GenerateOutput,
        default_slot=ModelSlot.GENERATOR,
    ),
    RoleTask.SCORE: TaskSpec(
        role="Critic",
        input_type=ScoreInput,
        output_type=ScoreOutput,
        default_slot=ModelSlot.CRITIC_DEEP,
    ),
    RoleTask.BASELINE: TaskSpec(
        role="Baseline",
        input_type=BaselineInput,
        output_type=BaselineForecast,
        default_slot=ModelSlot.BASELINE,
    ),
}
