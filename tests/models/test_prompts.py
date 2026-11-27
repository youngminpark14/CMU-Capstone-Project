import json
import re

import pytest

from pmfa.errors import ConfigError
from pmfa.models.roles import runner as runner_module
from pmfa.models.roles.prompts import Repair, render, task_text
from pmfa.models.roles.runner import DirectRunner, make_runner
from pmfa.models.roles.specs import INSTRUCTIONS, ROLES, role_for
from pmfa.records.roles import TASKS, RoleTask

from .fakes import FakeBackend
from .fixtures import INPUTS, PASSAGES, TRIGGER, make_config


def input_json(text: str) -> dict:
    return json.loads(re.search(r"<input>\n(.*)\n</input>", text, re.S).group(1))


def test_every_task_has_a_role_and_instructions():
    for task, spec in TASKS.items():
        assert role_for(task).name == spec.role
        assert task in INSTRUCTIONS
    assert set(ROLES) == {"Monitor", "Researcher", "Generator", "Critic", "Baseline"}


def test_passage_text_is_outside_the_json_inside_delimited_blocks():
    text = task_text(RoleTask.SELECT_PASSAGES, INPUTS[RoleTask.SELECT_PASSAGES])
    data = input_json(text)
    assert all("text" not in p for p in data["passages"])
    assert [p["passage_id"] for p in data["passages"]] == ["p1", "p2"]
    for passage in PASSAGES[:1]:
        block = re.search(
            r'<evidence passage_id="p1" tag="([0-9a-f]+)">\n(.*?)\n</evidence tag="\1">',
            text,
            re.S,
        )
        assert block and block.group(2) == passage.text


def test_the_trigger_excerpt_is_delimited_too():
    text = task_text(RoleTask.MATCH, INPUTS[RoleTask.MATCH])
    assert "excerpt" not in input_json(text)["trigger"]
    assert re.search(
        r'<evidence field="trigger.excerpt" tag="[0-9a-f]+">\n' + TRIGGER.excerpt, text
    )


def test_crewai_placeholders_in_untrusted_text_are_defused():
    text = task_text(RoleTask.SELECT_PASSAGES, INPUTS[RoleTask.SELECT_PASSAGES])
    assert "{tools}" not in text and "{ tools }" in text


def test_rendering_is_deterministic_and_carries_as_of():
    payload = INPUTS[RoleTask.GENERATE]
    assert render(RoleTask.GENERATE, payload) == render(RoleTask.GENERATE, payload)
    assert input_json(task_text(RoleTask.GENERATE, payload))["as_of"] == "2026-11-20T15:00:00Z"


def test_the_system_text_carries_the_rules():
    prompt = render(RoleTask.SCORE, INPUTS[RoleTask.SCORE])
    assert prompt.system.startswith("You are Critic.")
    assert "untrusted" in prompt.system and "market price" in prompt.system
    assert prompt.user.startswith("\nCurrent Task: Score the sibling scenarios")


def test_a_repair_shows_the_previous_answer_and_the_problem():
    repair = Repair(previous_answer='{"matched": "maybe"}', problem="matched must be a boolean")
    text = task_text(RoleTask.MATCH, INPUTS[RoleTask.MATCH], repair)
    assert '{"matched": "maybe"}' in text and "matched must be a boolean" in text


def test_the_wrong_input_record_is_refused():
    with pytest.raises(TypeError):
        task_text(RoleTask.MATCH, INPUTS[RoleTask.SCORE])


def test_runner_selection(monkeypatch):
    backend = FakeBackend([])
    assert isinstance(
        make_runner(make_config(models={"roles_runner": "gateway"}), backend), DirectRunner
    )
    monkeypatch.setattr(runner_module, "crewai_available", lambda: False)
    assert isinstance(make_runner(make_config(), backend), DirectRunner)  # auto, no CrewAI
    with pytest.raises(ConfigError, match="crewai is not installed"):
        make_runner(make_config(models={"roles_runner": "crewai"}), backend)
