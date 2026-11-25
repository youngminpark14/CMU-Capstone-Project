"""The model gateway: every live model call goes through ``Gateway.call``.

Role and input record in, validated output record out. The gateway owns:

- the map from ``ModelSlot`` to model, effort, and max_tokens (``config.models.slots``;
  a task's default slot comes from ``records.roles.TASKS`` unless the caller names one);
- validation against the task's output record, with one repair retry that shows
  the model its unusable answer and what was wrong with it; a refusal or a
  truncated answer gets the same single retry; a second failure raises
  ``ModelOutputError``;
- the meters and hard caps (``pmfa.models.meter``), checked before every call;
- the content-addressed response cache (``pmfa.models.cache``);
- cassette recording (``pmfa.models.cassette``).

Roles are run by a ``RoleRunner`` (CrewAI or direct), which sends the prompt to
a ``Backend``. Replay worlds use ``CassettePlayer`` and tests ``ScriptedModels``
instead of this class; all three implement the ``Models`` port.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pmfa.config import Config, ModelAssignment
from pmfa.errors import BudgetExhausted, ModelOutputError, RunBudgetExhausted
from pmfa.models.backend import AnthropicBackend, Backend, Completion
from pmfa.models.cache import CachedAnswer, ResponseCache, cache_key
from pmfa.models.cassette import CassetteRecorder, EntryUsage
from pmfa.models.meter import Meter, usage_of
from pmfa.models.pricing import cost
from pmfa.models.roles.prompts import Repair, render
from pmfa.models.roles.runner import RoleRunner, make_runner
from pmfa.records.base import Record
from pmfa.records.episode import CallMeta, ModelResult, Usage
from pmfa.records.roles import TASKS, ModelSlot, RoleTask

_MAX_PROBLEM_CHARS = 2000


def _problem(completion: Completion, output_type: type[Record]) -> tuple[Record | None, str]:
    """The validated record, or None and a plain description of what was wrong."""
    if completion.stop_reason == "refusal":
        return None, "the model declined to answer (stop_reason refusal)"
    if completion.stop_reason == "max_tokens":
        return None, (
            "the answer was cut off at the max_tokens limit before it was complete; "
            "answer more briefly"
        )
    if isinstance(completion.parsed, output_type):
        return completion.parsed, ""
    try:
        return output_type.model_validate_json(completion.raw_text), ""
    except ValidationError as exc:
        text = f"the answer did not validate as {output_type.__name__}: {exc}"
        return None, text[:_MAX_PROBLEM_CHARS]


class Gateway:
    """The live ``Models`` port: slots, validation and repair, meters and caps, cache, cassettes."""

    def __init__(
        self,
        config: Config,
        *,
        runner: RoleRunner | None = None,
        backend: Backend | None = None,
        cache_dir: str | Path | None = None,
        recorder: CassetteRecorder | None = None,
        enforce_seconds: bool = False,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a gateway.

        ``runner`` defaults to the one ``config.models.roles_runner`` selects,
        over ``backend`` (default: ``AnthropicBackend``). ``cache_dir`` turns the
        response cache on. ``recorder`` records cassettes. ``enforce_seconds``
        turns on the per-episode seconds cap (the live modes set it).
        """
        self.config = config
        if runner is None:
            runner = make_runner(config, backend or AnthropicBackend.from_config(config))
        self.runner = runner
        self.cache = ResponseCache(cache_dir) if cache_dir is not None else None
        self.recorder = recorder
        self.meter = Meter(config.budget, enforce_seconds=enforce_seconds)
        self._timer = timer

    # ------------------------------------------------------------------ the port

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        spec = TASKS[task]
        if not isinstance(payload, spec.input_type):
            raise TypeError(
                f"task {task.value} takes {spec.input_type.__name__}, got {type(payload).__name__}"
            )
        chosen = slot or spec.default_slot
        assignment = self.config.models.slots[chosen]
        entry: dict[str, Any] = {
            "task": task,
            "slot": chosen,
            "role": spec.role,
            "model": assignment.model,
            "effort": assignment.effort,
            "input": payload.model_dump(mode="json"),
        }
        self._check(episode_id, entry)

        prompt = render(task, payload)
        key = cache_key(
            model=assignment.model,
            effort=assignment.effort,
            max_tokens=assignment.max_tokens,
            system=prompt.system,
            user=prompt.user,
            output_type=spec.output_type,
        )
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return self._from_cache(hit, episode_id, entry)

        attempts = [self._attempt(task, payload, assignment, episode_id, None)]
        output, problem = _problem(attempts[0][0], spec.output_type)
        if output is None:
            first_problem = problem
            self._check(episode_id, entry, attempts)
            repair = Repair(previous_answer=attempts[0][0].raw_text, problem=problem)
            attempts.append(self._attempt(task, payload, assignment, episode_id, repair))
            output, problem = _problem(attempts[1][0], spec.output_type)
            if output is None:
                message = (
                    f"{task.value}: no usable answer after one repair retry. "
                    f"First: {first_problem} Then: {problem}"
                )
                self._record(
                    episode_id, entry, attempts, outcome="model_output_error", error=message
                )
                raise ModelOutputError(message)

        meta = self._meta(entry, attempts, source="live")
        if self.cache is not None:
            self.cache.put(
                CachedAnswer(
                    key=key,
                    task=task.value,
                    model=assignment.model,
                    served_model=meta.served_model,
                    effort=assignment.effort,
                    output=output.model_dump(mode="json"),
                    input_tokens=meta.input_tokens,
                    output_tokens=meta.output_tokens,
                    cost_usd=meta.cost_usd,
                    latency_ms=meta.latency_ms,
                    repaired=meta.repaired,
                )
            )
        self._record(episode_id, entry, attempts, output=output)
        return ModelResult(output=output, meta=meta)

    def usage(self, episode_id: str) -> Usage:
        return self.meter.episode(episode_id)

    def run_usage(self) -> Usage:
        """What the whole run has spent: new calls, tokens, dollars, and seconds."""
        return self.meter.run()

    # ------------------------------------------------------------------ helpers

    def _check(
        self,
        episode_id: str,
        entry: dict[str, Any],
        attempts: list[tuple[Completion, float]] | None = None,
    ) -> None:
        try:
            self.meter.check(episode_id)
        except (BudgetExhausted, RunBudgetExhausted) as exc:
            outcome = (
                "budget_exhausted" if isinstance(exc, BudgetExhausted) else "run_budget_exhausted"
            )
            self._record(episode_id, entry, attempts or [], outcome=outcome, error=str(exc))
            raise

    def _attempt(
        self,
        task: RoleTask,
        payload: Record,
        assignment: ModelAssignment,
        episode_id: str,
        repair: Repair | None,
    ) -> tuple[Completion, float]:
        started = self._timer()
        try:
            completion = self.runner.run(
                task,
                payload,
                assignment,
                timeout=self.config.models.request_timeout_seconds,
                repair=repair,
            )
        except BaseException:
            self.meter.add(episode_id, usage_of(seconds=max(0.0, self._timer() - started)))
            raise
        seconds = max(0.0, self._timer() - started)
        self.meter.add(
            episode_id,
            usage_of(
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                cost_usd=self._price(assignment.model, completion),
                seconds=seconds,
            ),
        )
        return completion, seconds

    def _price(self, requested: str, completion: Completion) -> Decimal:
        catalog = self.config.models.catalog
        model = completion.served_model if completion.served_model in catalog else requested
        return cost(model, completion.input_tokens, completion.output_tokens, catalog=catalog)

    def _meta(
        self, entry: dict[str, Any], attempts: list[tuple[Completion, float]], *, source: str
    ) -> CallMeta:
        last = attempts[-1][0]
        return CallMeta(
            task=entry["task"],
            slot=entry["slot"],
            role=entry["role"],
            model=entry["model"],
            served_model=last.served_model,
            effort=entry["effort"],
            input_tokens=sum(c.input_tokens for c, _ in attempts),
            output_tokens=sum(c.output_tokens for c, _ in attempts),
            cost_usd=sum((self._price(entry["model"], c) for c, _ in attempts), Decimal("0")),
            latency_ms=round(sum(s for _, s in attempts) * 1000),
            source=source,  # type: ignore[arg-type]
            repaired=len(attempts) > 1,
        )

    def _from_cache(self, hit: CachedAnswer, episode_id: str, entry: dict[str, Any]) -> ModelResult:
        spec = TASKS[entry["task"]]
        output = spec.output_type.model_validate(hit.output)
        # The episode is charged what the answer cost when it was made, so a rerun
        # from the cache meets its caps at the same point; the run pays nothing new.
        self.meter.add(
            episode_id,
            usage_of(
                input_tokens=hit.input_tokens,
                output_tokens=hit.output_tokens,
                cost_usd=hit.cost_usd,
                seconds=hit.latency_ms / 1000,
            ),
            run_delta=usage_of(),
        )
        meta = CallMeta(
            task=entry["task"],
            slot=entry["slot"],
            role=entry["role"],
            model=entry["model"],
            served_model=hit.served_model,
            effort=entry["effort"],
            input_tokens=hit.input_tokens,
            output_tokens=hit.output_tokens,
            cost_usd=hit.cost_usd,
            latency_ms=hit.latency_ms,
            source="cache",
            repaired=hit.repaired,
        )
        if self.recorder is not None:
            self.recorder.record(
                episode_id,
                **entry,
                served_model=hit.served_model,
                source="cache",
                output=output.model_dump(mode="json"),
                usage=EntryUsage(
                    input_tokens=hit.input_tokens,
                    output_tokens=hit.output_tokens,
                    cost_usd=hit.cost_usd,
                ),
                latency_ms=hit.latency_ms,
                repaired=hit.repaired,
                calls=1,
            )
        return ModelResult(output=output, meta=meta)

    def _record(
        self,
        episode_id: str,
        entry: dict[str, Any],
        attempts: list[tuple[Completion, float]],
        *,
        output: Record | None = None,
        outcome: str = "ok",
        error: str = "",
    ) -> None:
        if self.recorder is None:
            return
        meta = self._meta(entry, attempts, source="live") if attempts else None
        self.recorder.record(
            episode_id,
            **entry,
            served_model=meta.served_model if meta else "",
            source="live",
            output=output.model_dump(mode="json") if output is not None else None,
            outcome=outcome,
            error=error,
            usage=EntryUsage(
                input_tokens=meta.input_tokens if meta else 0,
                output_tokens=meta.output_tokens if meta else 0,
                cost_usd=meta.cost_usd if meta else Decimal("0"),
            ),
            latency_ms=meta.latency_ms if meta else 0,
            repaired=len(attempts) > 1,
            calls=len(attempts),
        )


__all__ = ["Gateway"]
