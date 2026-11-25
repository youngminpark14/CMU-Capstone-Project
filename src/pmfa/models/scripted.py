"""Scripted models for tests: the ``Models`` port answered from a fixed script.

A script maps each ``RoleTask`` to either a list of output records, consumed in
order across the run, or a callable ``(payload) -> output record``. A list item
or a callable may also be (or raise) an exception, such as ``ModelOutputError``,
to script a failed call. Every call received is recorded in ``calls``. The
per-episode call cap from the configuration is honoured, so budget exhaustion
can be tested.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pmfa.config import Config, default_config
from pmfa.errors import PmfaError
from pmfa.models.meter import Meter, usage_of
from pmfa.records.base import Record
from pmfa.records.episode import CallMeta, ModelResult, Usage
from pmfa.records.roles import TASKS, ModelSlot, RoleTask

ScriptItem = Record | BaseException
Script = Mapping[RoleTask, Sequence[ScriptItem] | Callable[[Record], Record]]


class ScriptExhausted(PmfaError):
    """A scripted model was called more often than its script allows."""


@dataclass(frozen=True)
class ScriptedCall:
    """One call a scripted model received."""

    task: RoleTask
    payload: Record
    episode_id: str
    slot: ModelSlot


class ScriptedModels:
    """Implements the ``Models`` port from a script, with the configured per-episode call cap."""

    def __init__(self, script: Script, *, config: Config | None = None) -> None:
        self.config = config or default_config()
        self._script = dict(script)
        self._positions: dict[RoleTask, int] = {}
        self._meter = Meter(self.config.budget)
        self._lock = threading.Lock()
        self.calls: list[ScriptedCall] = []

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        spec = TASKS[task]
        if not isinstance(payload, spec.input_type):
            raise TypeError(
                f"task {task.value} takes {spec.input_type.__name__}, got {type(payload).__name__}"
            )
        chosen = slot or spec.default_slot
        with self._lock:
            self._meter.check(episode_id)
            self.calls.append(ScriptedCall(task, payload, episode_id, chosen))
            self._meter.add(episode_id, usage_of())
            item = self._next(task, payload)
        if isinstance(item, BaseException):
            raise item
        if not isinstance(item, spec.output_type):
            raise TypeError(
                f"script for {task.value} gave {type(item).__name__}, expected "
                f"{spec.output_type.__name__}"
            )
        assignment = self.config.models.slots[chosen]
        meta = CallMeta(
            task=task,
            slot=chosen,
            role=spec.role,
            model=assignment.model,
            served_model=assignment.model,
            effort=assignment.effort,
            source="scripted",
        )
        return ModelResult(output=item, meta=meta)

    def _next(self, task: RoleTask, payload: Record) -> ScriptItem:
        entry = self._script.get(task)
        if entry is None:
            raise ScriptExhausted(f"the script has no answers for task {task.value}")
        if callable(entry):
            return entry(payload)  # a scripted failure raises straight through
        position = self._positions.get(task, 0)
        if position >= len(entry):
            raise ScriptExhausted(
                f"the script for task {task.value} ran out after {len(entry)} answers"
            )
        self._positions[task] = position + 1
        return entry[position]

    def usage(self, episode_id: str) -> Usage:
        return self._meter.episode(episode_id)

    def calls_for(self, task: RoleTask) -> list[ScriptedCall]:
        return [c for c in self.calls if c.task == task]


__all__ = ["Script", "ScriptExhausted", "ScriptItem", "ScriptedCall", "ScriptedModels"]
