"""Cassettes: recorded model calls, replayed offline in order.

A cassette is one readable JSON file per episode holding an ordered list of
entries: sequence number, task, slot, role, model, effort, the input record, the
output record, usage, and latency. Calls that ended without an answer (a
budget cap, or an answer that failed validation twice) are recorded too, so a
replay reproduces the episode's exits exactly.

``CassettePlayer`` implements the ``Models`` port from cassettes. Entries are
consumed in order per episode; the task and the input record must equal what
was recorded, otherwise ``CassetteMiss`` names the first difference. No network,
no key.
"""

from __future__ import annotations

import json
import re
import threading
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from pmfa.errors import BudgetExhausted, CassetteMiss, ModelOutputError, RunBudgetExhausted
from pmfa.models.meter import add_usage, usage_of
from pmfa.records.base import Record
from pmfa.records.episode import CallMeta, ModelResult, Usage
from pmfa.records.roles import TASKS, ModelSlot, RoleTask

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.ports import Models

Outcome = Literal["ok", "model_output_error", "budget_exhausted", "run_budget_exhausted"]

_ERRORS: dict[str, type[Exception]] = {
    "model_output_error": ModelOutputError,
    "budget_exhausted": BudgetExhausted,
    "run_budget_exhausted": RunBudgetExhausted,
}


class EntryUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")


class CassetteEntry(BaseModel):
    """One recorded model call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    """1-based position within the episode."""
    task: RoleTask
    slot: ModelSlot
    role: str
    model: str
    served_model: str = ""
    effort: str
    source: str = "live"
    """Where the recorded answer came from: ``live``, ``cache``, or ``scripted``."""
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    outcome: Outcome = "ok"
    error: str = ""
    usage: EntryUsage = EntryUsage()
    latency_ms: int = 0
    repaired: bool = False
    calls: int = 1
    """Model calls this entry charged to the episode's meter (2 after a repair retry)."""


class Cassette(BaseModel):
    """All recorded calls of one episode."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    episode_id: str
    entries: list[CassetteEntry] = []


def _file_name(episode_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", episode_id) + ".json"


def write_cassette(cassette: Cassette, directory: str | Path) -> Path:
    """Write a cassette as indented JSON to ``<directory>/<episode_id>.json``."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _file_name(cassette.episode_id)
    path.write_text(cassette.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_cassettes(*paths: str | Path) -> dict[str, Cassette]:
    """Cassettes by episode id, from files or from every ``*.json`` in directories."""
    found: dict[str, Cassette] = {}
    for item in paths:
        path = Path(item)
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for file in files:
            cassette = Cassette.model_validate_json(file.read_text(encoding="utf-8"))
            found[cassette.episode_id] = cassette
    return found


class CassetteRecorder:
    """Collects entries per episode and rewrites the episode's cassette file after each one."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._cassettes: dict[str, Cassette] = {}
        self._lock = threading.Lock()

    def record(self, episode_id: str, **fields: Any) -> CassetteEntry:
        with self._lock:
            cassette = self._cassettes.setdefault(episode_id, Cassette(episode_id=episode_id))
            entry = CassetteEntry(seq=len(cassette.entries) + 1, **fields)
            cassette.entries.append(entry)
            write_cassette(cassette, self.directory)
            return entry

    def cassette(self, episode_id: str) -> Cassette | None:
        with self._lock:
            return self._cassettes.get(episode_id)


def first_difference(recorded: Any, now: Any, path: str = "") -> str | None:
    """Where two JSON values first differ, as ``path: recorded X, now Y``; None when equal."""
    if isinstance(recorded, dict) and isinstance(now, dict):
        for key in list(recorded) + [k for k in now if k not in recorded]:
            sub = f"{path}.{key}" if path else str(key)
            if key not in now:
                return f"{sub}: recorded {_short(recorded[key])}, now missing"
            if key not in recorded:
                return f"{sub}: not recorded, now {_short(now[key])}"
            found = first_difference(recorded[key], now[key], sub)
            if found:
                return found
        return None
    if isinstance(recorded, list) and isinstance(now, list):
        for i, (a, b) in enumerate(zip(recorded, now, strict=False)):
            found = first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
        if len(recorded) != len(now):
            return f"{path or '<root>'}: recorded {len(recorded)} items, now {len(now)}"
        return None
    if recorded != now or type(recorded) is not type(now):
        return f"{path or '<root>'}: recorded {_short(recorded)}, now {_short(now)}"
    return None


def _short(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class CassettePlayer:
    """The ``Models`` port, answered from recorded cassettes."""

    def __init__(self, cassettes: dict[str, Cassette]) -> None:
        self._cassettes = cassettes
        self._positions: dict[str, int] = {}
        self._usage: dict[str, Usage] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_paths(cls, *paths: str | Path) -> CassettePlayer:
        return cls(load_cassettes(*paths))

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        spec = TASKS[task]
        if not isinstance(payload, spec.input_type):
            raise TypeError(
                f"task {task.value} takes {spec.input_type.__name__}, got {type(payload).__name__}"
            )
        with self._lock:
            cassette = self._cassettes.get(episode_id)
            position = self._positions.get(episode_id, 0)
            if cassette is None:
                raise CassetteMiss(f"no cassette for episode {episode_id}")
            if position >= len(cassette.entries):
                raise CassetteMiss(
                    f"episode {episode_id}: call #{position + 1} ({task.value}) is not on the "
                    f"cassette, which holds {len(cassette.entries)} calls"
                )
            entry = cassette.entries[position]
            if entry.task != task:
                raise CassetteMiss(
                    f"episode {episode_id}: call #{entry.seq} asks for {task.value}, the "
                    f"cassette recorded {entry.task.value}"
                )
            difference = first_difference(entry.input, payload.model_dump(mode="json"))
            if difference is not None:
                raise CassetteMiss(
                    f"episode {episode_id}: call #{entry.seq} ({task.value}) input differs "
                    f"from the recording at {difference}"
                )
            self._positions[episode_id] = position + 1
            if entry.calls:
                self._charge(episode_id, entry)
            if entry.outcome != "ok":
                raise _ERRORS[entry.outcome](entry.error or entry.outcome)
        output = spec.output_type.model_validate(entry.output)
        meta = CallMeta(
            task=task,
            slot=entry.slot,
            role=entry.role,
            model=entry.model,
            served_model=entry.served_model,
            effort=entry.effort,
            input_tokens=entry.usage.input_tokens,
            output_tokens=entry.usage.output_tokens,
            cost_usd=entry.usage.cost_usd,
            latency_ms=entry.latency_ms,
            source="cassette",
            repaired=entry.repaired,
        )
        return ModelResult(output=output, meta=meta)

    def _charge(self, episode_id: str, entry: CassetteEntry) -> None:
        delta = usage_of(
            calls=entry.calls,
            input_tokens=entry.usage.input_tokens,
            output_tokens=entry.usage.output_tokens,
            cost_usd=entry.usage.cost_usd,
            seconds=entry.latency_ms / 1000,
        )
        self._usage[episode_id] = add_usage(self._usage.get(episode_id, Usage()), delta)

    def usage(self, episode_id: str) -> Usage:
        with self._lock:
            return self._usage.get(episode_id, Usage())

    def remaining(self, episode_id: str) -> int:
        """Recorded calls of an episode not yet replayed (a faithful replay ends at 0)."""
        with self._lock:
            cassette = self._cassettes.get(episode_id)
            total = len(cassette.entries) if cassette else 0
            return total - self._positions.get(episode_id, 0)


class ModelTap:
    """The ``Models`` port, passed through, with a cassette per episode and counts by source.

    The cassettes are what makes a saved trace replayable offline; the counts
    tell the manifest how many answers were new and how many came from the cache.
    The backtest records its traces through it, and the example build script its
    hand-written answers.
    """

    def __init__(self, inner: Models, config: Config) -> None:
        self.inner = inner
        self.config = config
        self.cassettes: dict[str, Cassette] = {}
        self.sources: dict[str, int] = {}
        self.usage_total = Usage()

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        spec = TASKS[task]
        chosen = slot or spec.default_slot
        assignment = self.config.models.slots[chosen]
        base: dict[str, Any] = {
            "task": task,
            "slot": chosen,
            "role": spec.role,
            "model": assignment.model,
            "effort": assignment.effort,
            "input": payload.model_dump(mode="json"),
        }
        before = self.inner.usage(episode_id)
        try:
            result = self.inner.call(task, payload, episode_id=episode_id, slot=slot)
        except BudgetExhausted as exc:
            self._add(episode_id, base, outcome="budget_exhausted", error=str(exc), calls=0)
            raise
        except RunBudgetExhausted as exc:
            self._add(episode_id, base, outcome="run_budget_exhausted", error=str(exc), calls=0)
            raise
        except ModelOutputError as exc:
            after = self.inner.usage(episode_id)
            self._add(
                episode_id,
                base,
                outcome="model_output_error",
                error=str(exc),
                calls=max(1, after.calls - before.calls),
            )
            raise
        meta = result.meta
        self.sources[meta.source] = self.sources.get(meta.source, 0) + 1
        self.usage_total = Usage(
            calls=self.usage_total.calls + 1,
            input_tokens=self.usage_total.input_tokens + meta.input_tokens,
            output_tokens=self.usage_total.output_tokens + meta.output_tokens,
            cost_usd=self.usage_total.cost_usd
            + (meta.cost_usd if meta.source in ("live", "scripted") else Decimal("0")),
            seconds=self.usage_total.seconds + meta.latency_ms / 1000,
        )
        self._add(
            episode_id,
            {
                **base,
                "slot": meta.slot,
                "role": meta.role,
                "model": meta.model,
                "effort": meta.effort,
            },
            served_model=meta.served_model,
            source=meta.source,
            output=result.output.model_dump(mode="json"),
            usage=EntryUsage(
                input_tokens=meta.input_tokens,
                output_tokens=meta.output_tokens,
                cost_usd=meta.cost_usd,
            ),
            latency_ms=meta.latency_ms,
            repaired=meta.repaired,
            calls=2 if meta.repaired else 1,
        )
        return result

    def _add(self, episode_id: str, fields: dict[str, Any], **more: Any) -> None:
        cassette = self.cassettes.setdefault(episode_id, Cassette(episode_id=episode_id))
        cassette.entries.append(CassetteEntry(seq=len(cassette.entries) + 1, **fields, **more))

    def usage(self, episode_id: str) -> Usage:
        return self.inner.usage(episode_id)

    def cassette(self, episode_id: str) -> Cassette:
        return self.cassettes.get(episode_id, Cassette(episode_id=episode_id))


__all__ = [
    "Cassette",
    "CassetteEntry",
    "CassettePlayer",
    "CassetteRecorder",
    "EntryUsage",
    "ModelTap",
    "first_difference",
    "load_cassettes",
    "write_cassette",
]
