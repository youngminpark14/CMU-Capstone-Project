import pytest

from pmfa.errors import BudgetExhausted, ModelOutputError
from pmfa.models import ScriptedModels, ScriptExhausted
from pmfa.ports import Models
from pmfa.records.roles import MatchDecision, ModelSlot, RoleTask

from .fixtures import INPUTS, OUTPUTS, make_config

NO_MATCH = MatchDecision(matched=False, event_ticker="", rationale="Unrelated.")


def test_lists_are_consumed_in_order_and_calls_are_recorded():
    models = ScriptedModels({RoleTask.MATCH: [OUTPUTS[RoleTask.MATCH], NO_MATCH]})
    assert isinstance(models, Models)
    first = models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e1")
    second = models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e2")
    assert first.output.matched and not second.output.matched
    assert first.meta.source == "scripted" and first.meta.slot == ModelSlot.MONITOR
    assert [c.episode_id for c in models.calls] == ["e1", "e2"]
    assert models.usage("e1").calls == 1


def test_a_callable_answers_from_the_payload():
    models = ScriptedModels(
        {
            RoleTask.SCORE: lambda payload: OUTPUTS[RoleTask.SCORE],
            RoleTask.MATCH: lambda payload: NO_MATCH,
        }
    )
    result = models.call(
        RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e", slot=ModelSlot.CRITIC_DEPTH1
    )
    assert result.meta.slot == ModelSlot.CRITIC_DEPTH1
    assert result.meta.model == "claude-fable-5-2"
    assert models.calls_for(RoleTask.SCORE)[0].payload == INPUTS[RoleTask.SCORE]


def test_running_out_of_script_is_a_clear_error():
    models = ScriptedModels({RoleTask.MATCH: [NO_MATCH]})
    models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e")
    with pytest.raises(ScriptExhausted, match="match ran out after 1"):
        models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e")
    with pytest.raises(ScriptExhausted, match="no answers for task generate"):
        models.call(RoleTask.GENERATE, INPUTS[RoleTask.GENERATE], episode_id="e")


def test_scripted_failures_are_raised():
    models = ScriptedModels({RoleTask.MATCH: [ModelOutputError("malformed twice")]})
    with pytest.raises(ModelOutputError):
        models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e")


def test_wrong_types_are_rejected():
    models = ScriptedModels({RoleTask.MATCH: [OUTPUTS[RoleTask.PLAN_QUERIES]]})
    with pytest.raises(TypeError):
        models.call(RoleTask.MATCH, INPUTS[RoleTask.PLAN_QUERIES], episode_id="e")
    with pytest.raises(TypeError, match="expected MatchDecision"):
        models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e")


def test_the_per_episode_call_cap_is_honoured():
    config = make_config(budget={"max_calls_per_episode": 4})
    models = ScriptedModels({RoleTask.MATCH: lambda payload: NO_MATCH}, config=config)
    for _ in range(4):
        models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e")
    with pytest.raises(BudgetExhausted):
        models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="e")
    assert len(models.calls) == 4
    # Another episode has its own budget.
    models.call(RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="other")
