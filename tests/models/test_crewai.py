"""The CrewAI runner, run in a subprocess with the network refused (see ``crewai_probe.py``)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pmfa.models.roles.runner import crewai_available
from pmfa.records.roles import RoleTask

pytestmark = [
    pytest.mark.crewai,
    pytest.mark.skipif(not crewai_available(), reason="crewai is not installed"),
]

REPO = Path(__file__).resolve().parents[2]
CLOSED_PORT_PROXY = "http://127.0.0.1:9"


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    home = tmp_path_factory.mktemp("crewai-home")
    out = home / "report.json"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("CREWAI_", "OTEL_", "NO_PROXY", "LANGSMITH", "LANGCHAIN"))
    }
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": str(REPO),
            "HTTP_PROXY": CLOSED_PORT_PROXY,
            "HTTPS_PROXY": CLOSED_PORT_PROXY,
            "ALL_PROXY": CLOSED_PORT_PROXY,
            "http_proxy": CLOSED_PORT_PROXY,
            "https_proxy": CLOSED_PORT_PROXY,
            "all_proxy": CLOSED_PORT_PROXY,
        }
    )
    done = subprocess.run(
        [sys.executable, str(REPO / "tests" / "models" / "crewai_probe.py"), str(out)],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=110,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return {
        "report": json.loads(out.read_text()),
        "attempts_at_exit": json.loads(Path(f"{out}.network").read_text()),
        "stdout": done.stdout,
        "stderr": done.stderr,
    }


def test_one_llm_call_per_kickoff_and_the_record_comes_back_unchanged(probe):
    tasks = probe["report"]["tasks"]
    assert set(tasks) == {t.value for t in RoleTask} | {"match+repair"}
    for name, result in tasks.items():
        assert result["backend_calls"] == 1, name
        assert result["same_instance"], name


def test_crewai_and_direct_runners_send_byte_identical_text(probe):
    for name, result in probe["report"]["tasks"].items():
        assert result["system_equal"], name
        assert result["user_equal"], name


def test_the_slot_reaches_the_backend(probe):
    score = probe["report"]["tasks"]["score"]
    assert (score["model"], score["effort"], score["output_type"]) == (
        "claude-sonnet-5-5",
        "high",
        "ScoreOutput",
    )


def test_an_unusable_answer_comes_back_without_extra_calls(probe):
    invalid = probe["report"]["invalid"]
    assert invalid == {"backend_calls": 1, "parsed_is_none": True, "raw_text": "not json"}


def test_backend_errors_propagate_without_crewai_retries(probe):
    error = probe["report"]["error"]
    assert error["raised"].startswith("429") and error["backend_calls"] == 1


def test_runs_inside_an_event_loop(probe):
    assert probe["report"]["event_loop"]["same_instance"]


@pytest.fixture(scope="module")
def hostile_env_report(tmp_path_factory):
    """What CrewAI switched on when the inherited environment and a .env file asked for tracing."""
    home = tmp_path_factory.mktemp("crewai-hostile")
    out = home / "report.json"
    dotenv_file = home / ".env"
    dotenv_file.write_text(
        "PMFA_PROBE_FROM_DOTENV=read-from-dotenv\nCREWAI_TRACING_ENABLED=true\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON_DOTENV")}
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": str(REPO),
            "PMFA_PROBE_DOTENV": str(dotenv_file),
            "CREWAI_TRACING_ENABLED": "true",
            "CREWAI_DISABLE_TELEMETRY": "false",
            "CREWAI_DISABLE_TRACKING": "false",
            "OTEL_SDK_DISABLED": "false",
            "HTTP_PROXY": CLOSED_PORT_PROXY,
            "HTTPS_PROXY": CLOSED_PORT_PROXY,
            "http_proxy": CLOSED_PORT_PROXY,
            "https_proxy": CLOSED_PORT_PROXY,
        }
    )
    env.pop("PMFA_PROBE_FROM_DOTENV", None)
    done = subprocess.run(
        [sys.executable, str(REPO / "tests" / "models" / "crewai_env_probe.py"), str(out)],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=110,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(out.read_text())


def test_inherited_variables_cannot_turn_crewai_tracing_back_on(hostile_env_report):
    assert hostile_env_report["tracing_enabled"] is False
    assert hostile_env_report["env"]["CREWAI_TRACING_ENABLED"] == "false"
    assert hostile_env_report["env"]["CREWAI_DISABLE_TELEMETRY"] == "true"
    assert hostile_env_report["env"]["OTEL_SDK_DISABLED"] == "true"


def test_crewai_does_not_load_a_dotenv_file(hostile_env_report):
    # Secrets come from the environment only; CrewAI's import-time load_dotenv() is switched off.
    assert hostile_env_report["from_dotenv"] is None
    assert hostile_env_report["env"]["PYTHON_DOTENV_DISABLED"] == "1"


def test_kickoff_makes_no_network_call_and_prints_nothing(probe):
    # The probe refuses and records every socket connection; with CrewAI's telemetry
    # left on, it records the exporter's attempts at exit, so an empty list means none.
    report = probe["report"]
    assert report["network_attempts"] == []
    assert probe["attempts_at_exit"] == []  # telemetry exporters flush at exit; none ran
    assert report["env"]["CREWAI_DISABLE_TELEMETRY"] == "true"
    assert report["env"]["CREWAI_TRACING_ENABLED"] == "false"
    assert probe["stdout"] == ""
