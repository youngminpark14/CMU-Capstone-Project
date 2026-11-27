"""The episode trace: thought, action, observation, in order, written to the store.

Every pipeline step logs what the role said (a *thought*: its one-or-two-sentence
rationale with the call's ``CallMeta``), what code executed (an *action*), and
what came back (an *observation*). The replay command prints this trace, so
summaries are short and factual and the records involved go into
``TraceStep.data`` as plain JSON.

``TracingModels`` wraps the ``Models`` port so every model call, including
the ones the forecast engine makes, leaves a thought in the trace.
"""

from __future__ import annotations

from typing import Any

from pmfa.ports import Clock, Models, StateStore
from pmfa.records.base import Record
from pmfa.records.episode import ModelResult, TraceStep, Usage
from pmfa.records.roles import (
    GenerateOutput,
    MatchDecision,
    ModelSlot,
    PassageSelection,
    QueryPlan,
    RoleTask,
    ScoreOutput,
)

SUMMARY_CHARS = 280
"""Summaries are cut to this length: the trace is read by people, not parsed."""


def short(text: str, limit: int = SUMMARY_CHARS) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def dump(record: Record) -> dict[str, Any]:
    """A record as plain JSON."""
    return record.model_dump(mode="json")


class Tracer:
    """Appends trace steps for one store, numbering each episode's steps from 1."""

    def __init__(self, store: StateStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self._seq: dict[str, int] = {}

    def step(
        self,
        episode_id: str,
        node: str,
        kind: str,
        summary: str,
        *,
        role: str | None = None,
        data: dict[str, Any] | None = None,
        result: ModelResult | None = None,
    ) -> None:
        if episode_id not in self._seq:
            self._seq[episode_id] = len(self.store.get_trace(episode_id))
        self._seq[episode_id] += 1
        self.store.append_trace(
            episode_id,
            TraceStep(
                seq=self._seq[episode_id],
                node=node,
                kind=kind,  # type: ignore[arg-type]
                summary=short(summary),
                role=role,
                data=data or {},
                call=result.meta if result is not None else None,
                at=self.clock.now(),
            ),
        )

    def thought(self, episode_id: str, node: str, result: ModelResult) -> None:
        output = result.output
        self.step(
            episode_id,
            node,
            "thought",
            rationale_of(output),
            role=result.meta.role,
            data={"output": dump(output)} if isinstance(output, Record) else {},
            result=result,
        )

    def action(self, episode_id: str, node: str, summary: str, **data: Any) -> None:
        self.step(episode_id, node, "action", summary, data=data)

    def observation(self, episode_id: str, node: str, summary: str, **data: Any) -> None:
        self.step(episode_id, node, "observation", summary, data=data)

    def forget(self, episode_id: str) -> None:
        self._seq.pop(episode_id, None)


def rationale_of(output: object) -> str:
    """The role's short rationale for one output record."""
    if isinstance(output, MatchDecision | QueryPlan | PassageSelection):
        return output.rationale
    if isinstance(output, GenerateOutput):
        if not output.thoughts:
            return "Proposed no scenarios."
        claims = "; ".join(t.claim for t in output.thoughts)
        return f"Proposed {len(output.thoughts)} scenario(s): {claims}"
    if isinstance(output, ScoreOutput):
        parts = [f"{s.verdict} {s.probability:.2f}: {s.rationale}" for s in output.scores]
        return "Scored " + " | ".join(parts) if parts else "Scored nothing."
    rationale = getattr(output, "rationale", "")
    return str(rationale) or type(output).__name__


class TracingModels:
    """The ``Models`` port, writing a thought to the trace after every successful call.

    ``node`` is the pipeline step the thought is logged under; the controller sets
    it before each step runs.
    """

    def __init__(self, models: Models, tracer: Tracer) -> None:
        self.models = models
        self.tracer = tracer
        self.node = "match"

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        result = self.models.call(task, payload, episode_id=episode_id, slot=slot)
        self.tracer.thought(episode_id, self.node, result)
        return result

    def usage(self, episode_id: str) -> Usage:
        return self.models.usage(episode_id)


__all__ = ["Tracer", "TracingModels", "dump", "rationale_of", "short"]
