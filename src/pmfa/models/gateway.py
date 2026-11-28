"""The model gateway: every live model call goes through ``Gateway.call``.

Role and input record in, validated output record out. The gateway owns:

- the map from ``ModelSlot`` to model, effort, and max_tokens (``config.models.slots``;
  a task's default slot comes from ``records.roles.TASKS`` unless the caller names one);
- validation against the task's output record, with one repair retry that shows
  the model its unusable answer and what was wrong with it; a refusal, a
  truncated answer, or an answer that fails its input check (``INPUT_CHECKS``:
  a ``score`` answer must score each scenario asked for exactly once) gets the
  same single retry; a second failure raises ``ModelOutputError``;
- the meters and hard caps (``pmfa.models.meter``), checked before every call.
  A call's cost is known only once it returns, so each live call (the repair
  retry included) is checked at its worst case: ``input_token_bound`` of the
  rendered prompt times the assigned model's input price, plus ``max_tokens``
  times its output price. A call that could take the episode or run dollar cap
  past its limit is refused, so the caps are ceilings as far as the input-token
  bound holds (and the model served is the one assigned). A cached answer costs
  the run nothing new and is checked as any answer was before: against what was
  already spent. When the seconds cap is enforced (live modes), each call's
  timeout is the configured ``request_timeout_seconds`` or the episode's seconds
  left, whichever is shorter;
- the content-addressed response cache (``pmfa.models.cache``); a cached answer
  that fails its input check is treated as not cached;
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
from pmfa.errors import (
    BudgetExhausted,
    ModelCallFailed,
    ModelOutputError,
    PmfaError,
    RunBudgetExhausted,
)
from pmfa.models.backend import AnthropicBackend, Backend, Completion
from pmfa.models.cache import CachedAnswer, ResponseCache, cache_key
from pmfa.models.cassette import CassetteRecorder, EntryUsage
from pmfa.models.meter import Meter, usage_of
from pmfa.models.pricing import cost
from pmfa.models.roles.prompts import Prompt, Repair, render
from pmfa.models.roles.runner import RoleRunner, make_runner
from pmfa.records.base import Record
from pmfa.records.episode import CallMeta, ModelResult, Usage
from pmfa.records.roles import TASKS, ModelSlot, RoleTask, ScoreInput, ScoreOutput

_MAX_PROBLEM_CHARS = 2000

INPUT_OVERHEAD_TOKENS = 1000
"""Allowance for request framing beyond the system text, user text, and output schema."""


def input_token_bound(prompt: Prompt, output_type: type[Record]) -> int:
    """An upper estimate of a call's input tokens, for its worst-case cost.

    A token covers at least one byte of text, so the UTF-8 bytes of the system text,
    the user text, and the output record's JSON schema bound the tokens they make;
    ``INPUT_OVERHEAD_TOKENS`` covers the request's own framing.
    """
    import json

    schema = json.dumps(output_type.model_json_schema())
    text = prompt.system + prompt.user + schema
    return len(text.encode("utf-8")) + INPUT_OVERHEAD_TOKENS


def _one_score_each(payload: ScoreInput, output: ScoreOutput) -> str:
    """Why a score answer does not score each scenario asked for exactly once, or ""."""
    asked = [s.scenario_id for s in payload.scenarios]
    answered = [s.scenario_id for s in output.scores]
    if sorted(answered) == sorted(asked):
        return ""
    return (
        f"the answer must hold exactly one score for each scenario: asked for {asked}, "
        f"answered {answered}"
    )


INPUT_CHECKS: dict[RoleTask, Callable[[Any, Any], str]] = {RoleTask.SCORE: _one_score_each}
"""Checks a schema cannot state because they need the input: (input, output) -> problem or ""."""


def _input_problem(task: RoleTask, payload: Record, output: Record) -> str:
    check = INPUT_CHECKS.get(task)
    return check(payload, output) if check is not None else ""


def _problem(completion: Completion, task: RoleTask, payload: Record) -> tuple[Record | None, str]:
    """The usable record, or None and a plain description of what was wrong."""
    output_type = TASKS[task].output_type
    if completion.stop_reason == "refusal":
        return None, "the model declined to answer (stop_reason refusal)"
    if completion.stop_reason == "max_tokens":
        return None, (
            "the answer was cut off at the max_tokens limit before it was complete; "
            "answer more briefly"
        )
    if isinstance(completion.parsed, output_type):
        output: Record = completion.parsed
    else:
        try:
            output = output_type.model_validate_json(completion.raw_text)
        except ValidationError as exc:
            text = f"the answer did not validate as {output_type.__name__}: {exc}"
            return None, text[:_MAX_PROBLEM_CHARS]
    problem = _input_problem(task, payload, output)
    return (None, problem[:_MAX_PROBLEM_CHARS]) if problem else (output, "")


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
        prompt = render(task, payload)
        key = cache_key(
            model=assignment.model,
            effort=assignment.effort,
            max_tokens=assignment.max_tokens,
            system=prompt.system,
            user=prompt.user,
            output_type=spec.output_type,
        )
        hit = self.cache.get(key) if self.cache is not None else None
        if hit is not None and _input_problem(
            task, payload, spec.output_type.model_validate(hit.output)
        ):
            hit = None  # stored before the input check existed: ask the model again
        if hit is not None:
            self._check(episode_id, entry)  # nothing new to pay: what is spent decides
            return self._from_cache(hit, episode_id, entry)

        self._check(episode_id, entry, worst=self._worst_case(spec.output_type, assignment, prompt))
        before = self.meter.episode(episode_id)
        attempts: list[tuple[Completion, float]] = []
        try:
            attempts.append(self._attempt(task, payload, assignment, episode_id, None))
        except ModelCallFailed as exc:
            self._record_failed(episode_id, entry, before, exc)
            raise
        output, problem = _problem(attempts[0][0], task, payload)
        if output is None:
            first_problem = problem
            repair = Repair(previous_answer=attempts[0][0].raw_text, problem=problem)
            retry = render(task, payload, repair)
            self._check(
                episode_id,
                entry,
                attempts,
                worst=self._worst_case(spec.output_type, assignment, retry),
            )
            try:
                attempts.append(self._attempt(task, payload, assignment, episode_id, repair))
            except ModelCallFailed as exc:
                self._record_failed(episode_id, entry, before, exc)
                raise
            output, problem = _problem(attempts[1][0], task, payload)
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
        *,
        worst: Decimal = Decimal("0"),
    ) -> None:
        try:
            self.meter.check(episode_id, worst)
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
        timeout = self.config.models.request_timeout_seconds
        left = self.meter.seconds_left(episode_id)
        if left is not None:
            timeout = min(timeout, left)
        started = self._timer()
        try:
            completion = self.runner.run(
                task,
                payload,
                assignment,
                timeout=timeout,
                repair=repair,
            )
        except BaseException as exc:
            self.meter.add(episode_id, usage_of(seconds=self._elapsed(started)))
            if isinstance(exc, Exception) and not isinstance(exc, PmfaError):
                # A timeout or server error from the API client ends the episode, not the run.
                raise ModelCallFailed(
                    f"{task.value}: the model call failed: {type(exc).__name__}: {exc}"
                ) from exc
            raise
        seconds = self._elapsed(started)
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

    def _elapsed(self, started: float) -> float:
        """Seconds since ``started``, to the millisecond.

        A cassette and the cache store a call's latency in whole milliseconds. Measuring
        each attempt in that unit makes a replay charge exactly what the live call did.
        """
        return round(max(0.0, self._timer() - started), 3)

    def _worst_case(
        self, output_type: type[Record], assignment: ModelAssignment, prompt: Prompt
    ) -> Decimal:
        """The most one call with ``prompt`` can cost on the assigned model."""
        return cost(
            assignment.model,
            input_token_bound(prompt, output_type),
            assignment.max_tokens,
            catalog=self.config.models.catalog,
        )

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

    def _record_failed(
        self, episode_id: str, entry: dict[str, Any], before: Usage, error: ModelCallFailed
    ) -> None:
        """Put a call the API client failed on the cassette, with what it was charged.

        A replay then raises the same error at the same place and charges the same
        usage: the attempts made, the tokens of an earlier attempt, and the seconds.
        """
        if self.recorder is None:
            return
        after = self.meter.episode(episode_id)
        self.recorder.record(
            episode_id,
            **entry,
            served_model="",
            source="live",
            output=None,
            outcome="model_output_error",
            error=str(error),
            usage=EntryUsage(
                input_tokens=after.input_tokens - before.input_tokens,
                output_tokens=after.output_tokens - before.output_tokens,
                cost_usd=after.cost_usd - before.cost_usd,
            ),
            latency_ms=round((after.seconds - before.seconds) * 1000),
            repaired=False,
            calls=after.calls - before.calls,
        )


__all__ = ["INPUT_CHECKS", "INPUT_OVERHEAD_TOKENS", "Gateway", "input_token_bound"]
