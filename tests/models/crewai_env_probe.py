"""Load CrewAI the way the runner does, in a hostile environment, and report what it switched on.

Used by ``test_crewai.py`` in a subprocess. The parent starts this process with
CrewAI's telemetry and tracing switched on in the inherited environment, and
with ``PMFA_PROBE_DOTENV`` naming a ``.env`` file. ``dotenv.find_dotenv`` is
pointed at that file, which is what CrewAI's import-time ``load_dotenv()``
finds when a ``.env`` sits above the installed package (the project root, for
the default in-repository ``.venv``).

Usage: python crewai_env_probe.py OUTPUT.json
"""

from __future__ import annotations

import json
import os
import sys


def main(out_path: str) -> None:
    import dotenv.main

    dotenv_file = os.environ["PMFA_PROBE_DOTENV"]
    dotenv.main.find_dotenv = lambda *args, **kwargs: dotenv_file  # type: ignore[assignment]

    from pmfa.models.roles.crewai_runner import QUIET_ENV, _load_crewai

    _load_crewai()
    import crewai.llm  # noqa: F401  # the module that calls load_dotenv() on import
    from crewai.events.listeners.tracing.utils import should_enable_tracing

    report = {
        "env": {name: os.environ.get(name) for name in QUIET_ENV},
        "tracing_enabled": should_enable_tracing(),
        "from_dotenv": os.environ.get("PMFA_PROBE_FROM_DOTENV"),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)


if __name__ == "__main__":
    main(sys.argv[1])
