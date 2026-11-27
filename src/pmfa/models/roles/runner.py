"""Role runners: turn a role task and its input record into one backend call.

Two runners sit behind one interface. ``CrewAIRunner`` runs each role as a
single-agent CrewAI kickoff; ``DirectRunner`` renders the same text itself and
calls the backend. Both send byte-identical system and user text, so the
choice between them changes nothing a cassette or the cache can see.
"""

from __future__ import annotations

import importlib.util
from typing import Protocol

from pmfa.config import Config, ModelAssignment
from pmfa.errors import ConfigError
from pmfa.models.backend import Backend, Completion
from pmfa.models.roles.prompts import Repair, render
from pmfa.records.base import Record
from pmfa.records.roles import TASKS, RoleTask


class RoleRunner(Protocol):
    """Runs one role task as one model call."""

    name: str

    def run(
        self,
        task: RoleTask,
        payload: Record,
        assignment: ModelAssignment,
        *,
        timeout: float,
        repair: Repair | None = None,
    ) -> Completion: ...


class DirectRunner:
    """Calls the backend with the rendered prompt; no agent framework involved."""

    name = "direct"

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def run(
        self,
        task: RoleTask,
        payload: Record,
        assignment: ModelAssignment,
        *,
        timeout: float,
        repair: Repair | None = None,
    ) -> Completion:
        prompt = render(task, payload, repair)
        return self.backend.complete(
            system=prompt.system,
            user=prompt.user,
            output_type=TASKS[task].output_type,
            model=assignment.model,
            effort=assignment.effort,
            max_tokens=assignment.max_tokens,
            timeout=timeout,
        )


def crewai_available() -> bool:
    """Whether CrewAI is installed (checked without importing it)."""
    return importlib.util.find_spec("crewai") is not None


def make_runner(config: Config, backend: Backend) -> RoleRunner:
    """The runner ``config.models.roles_runner`` asks for: ``auto`` picks CrewAI when installed."""
    choice = config.models.roles_runner
    if choice == "gateway":
        return DirectRunner(backend)
    if choice == "crewai" or (choice == "auto" and crewai_available()):
        if not crewai_available():
            raise ConfigError(
                "models.roles_runner is 'crewai' but crewai is not installed "
                "(install the crewai dependency group, or set roles_runner to 'auto')"
            )
        from pmfa.models.roles.crewai_runner import CrewAIRunner

        return CrewAIRunner(backend)
    return DirectRunner(backend)


__all__ = ["DirectRunner", "RoleRunner", "crewai_available", "make_runner"]
