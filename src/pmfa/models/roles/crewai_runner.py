"""The CrewAI runner: each role is a ``crewai.Agent``, run as a single-agent kickoff.

The agent has no tools, no delegation, no memory, no planning, and a single
iteration. It is bound to the project's backend through ``crewai.BaseLLM``: the
adapter's ``call()`` receives CrewAI's messages and the response model, sends
exactly that system and user text to the backend, and returns the validated
record, which CrewAI 1.15 hands back unchanged after one LLM call.

When the backend's answer does not parse, or the backend raises, the adapter
does not return text to CrewAI (CrewAI would then make extra "convert this to
JSON" calls the gateway cannot meter). It keeps the completion or the error and
aborts the kickoff with an exception CrewAI's rate-limit retry does not match;
the runner hands the completion to the gateway, which owns the repair retry.

Telemetry, tracing, the version check, and console output are turned off before
CrewAI is first imported.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from typing import Any

from pmfa.config import ModelAssignment
from pmfa.models.backend import Backend, Completion
from pmfa.models.roles.prompts import Repair, task_text
from pmfa.models.roles.specs import ROLES, RoleSpec
from pmfa.records.base import Record
from pmfa.records.roles import TASKS, RoleTask

QUIET_ENV: dict[str, str] = {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "CREWAI_DISABLE_TRACKING": "true",
    "OTEL_SDK_DISABLED": "true",
    "CREWAI_TRACING_ENABLED": "false",
    "CREWAI_DISABLE_VERSION_CHECK": "true",
}
"""Set (unless already set) before CrewAI is imported."""

CONTEXT_WINDOW = 1_000_000
"""Reported to CrewAI so it never summarizes a long prompt behind the gateway's back."""

_adapter_class: type | None = None
_import_lock = threading.Lock()


class _KickoffAborted(Exception):
    """Stops a kickoff whose answer the gateway must handle. Its message matches no retry marker."""


def _load_crewai() -> type:
    """Import CrewAI quietly and build the adapter class once."""
    global _adapter_class
    with _import_lock:
        if _adapter_class is not None:
            return _adapter_class
        for name, value in QUIET_ENV.items():
            os.environ.setdefault(name, value)
        logging.getLogger("crewai").addHandler(logging.NullHandler())
        from crewai import BaseLLM
        from crewai.events.event_listener import EventListener
        from pydantic import PrivateAttr

        EventListener().formatter.verbose = False

        class GatewayLLM(BaseLLM):
            """CrewAI's view of the project backend: one call in, one validated record out."""

            _backend: Any = PrivateAttr(default=None)
            _assignment: Any = PrivateAttr(default=None)
            _timeout: float = PrivateAttr(default=0.0)
            _expected: Any = PrivateAttr(default=None)
            _outcome: Any = PrivateAttr(default=None)
            _calls: int = PrivateAttr(default=0)

            def call(
                self,
                messages: Any,
                tools: Any = None,
                callbacks: Any = None,
                available_functions: Any = None,
                from_task: Any = None,
                from_agent: Any = None,
                response_model: Any = None,
            ) -> Any:
                self._calls += 1
                completion: Completion | None = None
                failure: BaseException | None = None
                try:
                    if tools or available_functions:
                        raise RuntimeError("role agents have no tools")
                    if response_model is not self._expected:
                        raise RuntimeError(
                            f"CrewAI asked for {response_model!r}, expected {self._expected!r}"
                        )
                    system, user = split_messages(messages)
                    completion = self._backend.complete(
                        system=system,
                        user=user,
                        output_type=self._expected,
                        model=self._assignment.model,
                        effort=self._assignment.effort,
                        max_tokens=self._assignment.max_tokens,
                        timeout=self._timeout,
                    )
                except Exception as exc:
                    failure = exc
                # Raised outside the except block so the abort carries no __context__:
                # CrewAI's rate-limit retry walks the exception chain.
                if failure is not None:
                    self._outcome = failure
                    raise _KickoffAborted("backend call failed")
                assert completion is not None
                self._outcome = completion
                if completion.parsed is not None:
                    return completion.parsed
                raise _KickoffAborted("answer did not validate")

            def supports_function_calling(self) -> bool:
                return False

            def supports_stop_words(self) -> bool:
                return False

            def get_context_window_size(self) -> int:
                return CONTEXT_WINDOW

        _adapter_class = GatewayLLM
        return GatewayLLM


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return "" if content is None else str(content)


def split_messages(messages: Any) -> tuple[str, str]:
    """CrewAI's messages as one system text and one user text; anything else is an error."""
    if isinstance(messages, str):
        raise RuntimeError("expected CrewAI to send a system and a user message")
    systems = [m for m in messages if m.get("role") == "system"]
    users = [m for m in messages if m.get("role") == "user"]
    if len(systems) != 1 or len(users) != 1 or len(messages) != 2:
        roles = [m.get("role") for m in messages]
        raise RuntimeError(f"expected one system and one user message from CrewAI, got {roles}")
    return _content_text(systems[0].get("content")), _content_text(users[0].get("content"))


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class CrewAIRunner:
    """One ``crewai.Agent`` per role, each run as a single-agent kickoff."""

    name = "crewai"

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._agents: dict[str, tuple[Any, Any]] = {}
        self._lock = threading.Lock()

    def _agent(self, role: RoleSpec) -> tuple[Any, Any]:
        if role.name not in self._agents:
            adapter_class = _load_crewai()
            from crewai import Agent

            llm = adapter_class(model="pmfa-gateway")
            llm._backend = self.backend
            agent = Agent(
                role=role.name,
                goal=role.goal,
                backstory=role.backstory,
                llm=llm,
                tools=[],
                allow_delegation=False,
                max_iter=1,
                max_retry_limit=0,
                respect_context_window=False,
                memory=False,
                planning=False,
                cache=False,
                inject_date=False,
                verbose=False,
            )
            self._agents[role.name] = (agent, llm)
        return self._agents[role.name]

    def run(
        self,
        task: RoleTask,
        payload: Record,
        assignment: ModelAssignment,
        *,
        timeout: float,
        repair: Repair | None = None,
    ) -> Completion:
        spec = TASKS[task]
        text = task_text(task, payload, repair)
        with self._lock:
            agent, llm = self._agent(ROLES[spec.role])
            llm._assignment = assignment
            llm._timeout = timeout
            llm._expected = spec.output_type
            llm._outcome = None
            llm._calls = 0
            if _in_event_loop():
                # kickoff() returns a coroutine inside a running loop; run it on a plain thread.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result, aborted = pool.submit(_kickoff, agent, text, spec.output_type).result()
            else:
                result, aborted = _kickoff(agent, text, spec.output_type)
            outcome, calls = llm._outcome, llm._calls
        if isinstance(outcome, BaseException):
            raise outcome
        if not isinstance(outcome, Completion):
            raise RuntimeError("CrewAI kickoff finished without calling the model")
        if calls != 1:
            raise RuntimeError(f"CrewAI made {calls} model calls for one kickoff; expected 1")
        if not aborted and (result is None or result.pydantic is not outcome.parsed):
            raise RuntimeError("CrewAI changed the validated record it was given")
        return outcome


def _kickoff(agent: Any, text: str, output_type: type[Record]) -> tuple[Any, bool]:
    """Run one kickoff with console output suppressed. Returns (result, aborted)."""
    from crewai_core.printer import set_suppress_console_output

    token = set_suppress_console_output(True)
    try:
        return agent.kickoff(text, response_format=output_type), False
    except _KickoffAborted:
        return None, True
    finally:
        token.var.reset(token)  # type: ignore[attr-defined]


__all__ = ["CONTEXT_WINDOW", "QUIET_ENV", "CrewAIRunner", "split_messages"]
