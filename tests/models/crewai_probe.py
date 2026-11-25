"""Run the CrewAI runner in a fresh process and report what it sent, as JSON.

Used by ``test_crewai.py`` in a subprocess, so CrewAI's global state, its files
under ``$HOME``, and the socket guard below stay out of the pytest process.
Every attempt to open a network connection is recorded and refused.

Usage: python crewai_probe.py OUTPUT.json
"""

from __future__ import annotations

import asyncio
import atexit
import json
import socket
import sys

ATTEMPTS: list[str] = []
ATTEMPTS_FILE = sys.argv[1] + ".network" if len(sys.argv) > 1 else None


@atexit.register  # registered first, so it runs last: after any exporter flushes at exit
def _write_attempts() -> None:
    if ATTEMPTS_FILE:
        with open(ATTEMPTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(ATTEMPTS, fh)


def _refuse(*args, **kwargs):
    ATTEMPTS.append(repr(args[:2]))
    raise OSError("network disabled in the CrewAI probe")


socket.socket.connect = _refuse  # type: ignore[method-assign]
socket.socket.connect_ex = _refuse  # type: ignore[method-assign]
socket.create_connection = _refuse  # type: ignore[assignment]
socket.getaddrinfo = _refuse  # type: ignore[assignment]


def main(out_path: str) -> None:
    import os

    from pmfa.config import Config
    from pmfa.models.roles.crewai_runner import QUIET_ENV, CrewAIRunner
    from pmfa.models.roles.prompts import Repair, render
    from pmfa.records.roles import TASKS, RoleTask
    from tests.models.fakes import FakeBackend, ok, raw
    from tests.models.fixtures import INPUTS, OUTPUTS

    config = Config()
    report: dict = {"tasks": {}}
    repair = Repair(previous_answer='{"matched": "maybe"}', problem="matched must be a boolean")
    cases = [(task, None) for task in RoleTask] + [(RoleTask.MATCH, repair)]
    for task, rep in cases:
        expected = OUTPUTS[task]
        backend = FakeBackend([ok(expected)])
        runner = CrewAIRunner(backend)
        assignment = config.models.slots[TASKS[task].default_slot]
        completion = runner.run(task, INPUTS[task], assignment, timeout=5.0, repair=rep)
        direct = render(task, INPUTS[task], rep)
        request = backend.requests[0]
        report["tasks"][task.value + ("+repair" if rep else "")] = {
            "backend_calls": len(backend.requests),
            "same_instance": completion.parsed is expected,
            "system_equal": request["system"] == direct.system,
            "user_equal": request["user"] == direct.user,
            "system": request["system"],
            "user": request["user"],
            "model": request["model"],
            "effort": request["effort"],
            "output_type": request["output_type"].__name__,
        }

    # An answer that does not validate comes back to the caller after one call.
    backend = FakeBackend([raw("not json")])
    runner = CrewAIRunner(backend)
    assignment = config.models.slots[TASKS[RoleTask.MATCH].default_slot]
    completion = runner.run(RoleTask.MATCH, INPUTS[RoleTask.MATCH], assignment, timeout=5.0)
    report["invalid"] = {
        "backend_calls": len(backend.requests),
        "parsed_is_none": completion.parsed is None,
        "raw_text": completion.raw_text,
    }

    # A backend error that looks like a rate limit is not retried by CrewAI.
    def throttled(request):
        raise RuntimeError("429 too many requests: rate limit")

    backend = FakeBackend([throttled, throttled, throttled])
    runner = CrewAIRunner(backend)
    try:
        runner.run(RoleTask.MATCH, INPUTS[RoleTask.MATCH], assignment, timeout=5.0)
        report["error"] = {"raised": None}
    except RuntimeError as exc:
        report["error"] = {"raised": str(exc), "backend_calls": len(backend.requests)}

    # Inside a running event loop (kickoff would return a coroutine there).
    backend = FakeBackend([ok(OUTPUTS[RoleTask.MATCH])])
    runner = CrewAIRunner(backend)

    async def in_loop():
        return runner.run(RoleTask.MATCH, INPUTS[RoleTask.MATCH], assignment, timeout=5.0)

    completion = asyncio.run(in_loop())
    report["event_loop"] = {"same_instance": completion.parsed is OUTPUTS[RoleTask.MATCH]}

    report["env"] = {name: os.environ.get(name) for name in QUIET_ENV}
    report["network_attempts"] = ATTEMPTS
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)


if __name__ == "__main__":
    main(sys.argv[1])
