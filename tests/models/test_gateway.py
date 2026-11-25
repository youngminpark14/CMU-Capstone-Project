import json
from decimal import Decimal

import pytest

from pmfa.errors import BudgetExhausted, CassetteMiss, ModelOutputError, RunBudgetExhausted
from pmfa.models import CassettePlayer, CassetteRecorder, Gateway
from pmfa.models.roles.runner import DirectRunner
from pmfa.ports import Models
from pmfa.records.roles import MatchDecision, ModelSlot, RoleTask

from .fakes import FakeBackend, ok, raw
from .fixtures import AS_OF, INPUTS, OUTPUTS, make_config

MATCH_IN = INPUTS[RoleTask.MATCH]
MATCH_OUT = OUTPUTS[RoleTask.MATCH]


class Ticker:
    """A fake monotonic timer: each call advances by ``step`` seconds."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def gateway(answers, *, config=None, **kwargs):
    backend = FakeBackend(list(answers))
    config = config or make_config()
    return Gateway(config, runner=DirectRunner(backend), **kwargs), backend


def test_a_valid_answer_comes_back_with_its_cost_and_slot():
    gw, backend = gateway([ok(MATCH_OUT, tokens=(10_000, 1_000))])
    assert isinstance(gw, Models)
    result = gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert result.output == MATCH_OUT
    meta = result.meta
    assert (meta.slot, meta.model, meta.effort, meta.source) == (
        ModelSlot.MONITOR,
        "claude-haiku-5-5",
        "low",
        "live",
    )
    assert meta.cost_usd == Decimal("0.0015")  # 10k x $0.10/M + 1k x $0.50/M
    assert not meta.repaired
    assert backend.requests[0]["max_tokens"] == 4000
    assert gw.usage("e").calls == 1 and gw.usage("e").cost_usd == Decimal("0.0015")


def test_a_named_slot_overrides_the_tasks_default():
    gw, backend = gateway([ok(OUTPUTS[RoleTask.SCORE])])
    result = gw.call(
        RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e", slot=ModelSlot.CRITIC_DEPTH1
    )
    assert result.meta.model == "claude-fable-5-2"
    assert backend.requests[0]["model"] == "claude-fable-5-2"
    assert backend.requests[0]["effort"] == "high"


def test_the_wrong_input_record_is_refused():
    gw, backend = gateway([])
    with pytest.raises(TypeError):
        gw.call(RoleTask.MATCH, INPUTS[RoleTask.PLAN_QUERIES], episode_id="e")
    assert backend.requests == []


def test_malformed_output_gets_one_repair_retry_showing_the_bad_answer():
    bad = '{"matched": "maybe", "event_ticker": "", "rationale": "x"}'
    gw, backend = gateway([raw(bad), ok(MATCH_OUT)])
    result = gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert result.output == MATCH_OUT and result.meta.repaired
    assert len(backend.requests) == 2 and gw.usage("e").calls == 2
    repair_text = backend.requests[1]["user"]
    assert bad in repair_text and "matched" in repair_text


def test_malformed_twice_is_a_model_output_error():
    gw, backend = gateway([raw("not json"), raw("still not json")])
    with pytest.raises(ModelOutputError):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 2


def test_a_failing_validator_counts_as_malformed():
    # Quantiles must be non-decreasing; the schema cannot say so, the validator does.
    bad = OUTPUTS[RoleTask.BASELINE].model_dump(mode="json")
    bad["quantiles"] = {"q05": 3, "q25": 2, "q50": 1, "q75": 0, "q95": -1}
    gw, _ = gateway([raw(json.dumps(bad)), raw(json.dumps(bad))])
    with pytest.raises(ModelOutputError, match="non-decreasing"):
        gw.call(RoleTask.BASELINE, INPUTS[RoleTask.BASELINE], episode_id="e")


def test_a_refusal_is_retried_once_then_a_model_output_error():
    gw, backend = gateway([raw("", stop_reason="refusal"), raw("", stop_reason="refusal")])
    with pytest.raises(ModelOutputError, match="declined"):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 2


def test_a_truncated_answer_is_not_accepted_even_if_it_parses():
    truncated = raw(MATCH_OUT.model_dump_json(), stop_reason="max_tokens")
    gw, _ = gateway([truncated, truncated])
    with pytest.raises(ModelOutputError, match="max_tokens"):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")


def test_the_call_cap_stops_before_calling():
    config = make_config(budget={"max_calls_per_episode": 4})
    gw, backend = gateway([ok(MATCH_OUT)] * 5, config=config)
    for _ in range(4):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(BudgetExhausted):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 4
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="another")


def test_a_repair_retry_also_needs_budget():
    config = make_config(budget={"max_calls_per_episode": 4})
    gw, backend = gateway([ok(MATCH_OUT)] * 3 + [raw("bad")] * 2, config=config)
    for _ in range(3):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    with pytest.raises(BudgetExhausted):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 4


def test_the_episode_dollar_cap_stops_once_spent():
    config = make_config(budget={"max_usd_per_episode": "0.10", "max_usd_per_run": "25"})
    # Each Sonnet call: 40k in x $2/M + 2k out x $10/M = $0.10.
    answer = ok(OUTPUTS[RoleTask.SCORE], tokens=(40_000, 2_000))
    gw, backend = gateway([answer, answer], config=config)
    gw.call(RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e")
    with pytest.raises(BudgetExhausted, match="dollar"):
        gw.call(RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e")
    assert len(backend.requests) == 1


def test_the_seconds_cap_applies_only_when_enforced():
    config = make_config(budget={"max_seconds_per_episode": 5})
    answers = [ok(MATCH_OUT)] * 3
    lax, _ = gateway(answers, config=config, timer=Ticker(10.0))
    lax.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    lax.call(RoleTask.MATCH, MATCH_IN, episode_id="e")  # 10 s spent, not enforced

    strict, backend = gateway(answers, config=config, timer=Ticker(10.0), enforce_seconds=True)
    strict.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert strict.usage("e").seconds == pytest.approx(10.0)
    with pytest.raises(BudgetExhausted, match="seconds"):
        strict.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 1


def test_the_run_dollar_cap_raises_run_budget_exhausted():
    config = make_config(budget={"max_usd_per_episode": "0.10", "max_usd_per_run": "0.10"})
    answer = ok(OUTPUTS[RoleTask.SCORE], tokens=(40_000, 2_000))  # $0.10
    gw, backend = gateway([answer, answer], config=config)
    gw.call(RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e1")
    with pytest.raises(RunBudgetExhausted):
        gw.call(RoleTask.SCORE, INPUTS[RoleTask.SCORE], episode_id="e2")
    assert len(backend.requests) == 1
    assert gw.run_usage().cost_usd == Decimal("0.10")


def test_cache_hit_skips_the_model_and_keeps_the_recorded_cost(tmp_path):
    first, _ = gateway([ok(MATCH_OUT, tokens=(10_000, 1_000))], cache_dir=tmp_path)
    original = first.call(RoleTask.MATCH, MATCH_IN, episode_id="e")

    second, backend2 = gateway([], cache_dir=tmp_path)
    cached = second.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert backend2.requests == []
    assert cached.output == original.output
    assert cached.meta.source == "cache"
    assert cached.meta.cost_usd == original.meta.cost_usd
    assert cached.meta.latency_ms == original.meta.latency_ms
    assert second.usage("e").calls == 1  # a cached answer counts as a call
    assert second.run_usage().cost_usd == Decimal("0")  # but the run pays nothing new
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_as_of_is_part_of_the_cache_key(tmp_path):
    later = MATCH_IN.model_copy(update={"as_of": AS_OF.replace(hour=16)})
    gw, backend = gateway([ok(MATCH_OUT), ok(MATCH_OUT)], cache_dir=tmp_path)
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    gw.call(RoleTask.MATCH, later, episode_id="e")
    assert len(backend.requests) == 2
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 2


def test_failed_answers_are_not_cached(tmp_path):
    gw, backend = gateway([raw("x"), raw("y"), ok(MATCH_OUT)], cache_dir=tmp_path)
    with pytest.raises(ModelOutputError):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="e")
    assert len(backend.requests) == 3


def test_cassette_records_then_replays(tmp_path):
    no_match = MatchDecision(matched=False, event_ticker="", rationale="No.")
    recorder = CassetteRecorder(tmp_path)
    config = make_config(budget={"max_calls_per_episode": 4})
    gw, _ = gateway(
        [ok(MATCH_OUT), raw("bad"), ok(no_match), raw("x"), raw("y"), raw("z")],
        config=config,
        recorder=recorder,
    )
    live = [gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1").output]
    live.append(gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1").output)  # repaired
    with pytest.raises(BudgetExhausted):  # 3 calls used; a 4th fits, its repair does not
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1")
    with pytest.raises(ModelOutputError):
        gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-2")

    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert files == ["ep-1.json", "ep-2.json"]

    player = CassettePlayer.from_paths(tmp_path)
    assert isinstance(player, Models)
    replayed = [player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1").output for _ in range(2)]
    assert replayed == live
    with pytest.raises(BudgetExhausted):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-1")
    with pytest.raises(ModelOutputError):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep-2")
    assert player.usage("ep-1").calls == gw.usage("ep-1").calls == 4
    assert player.usage("ep-1").cost_usd == gw.usage("ep-1").cost_usd
    assert player.remaining("ep-1") == 0


def test_replay_with_a_changed_input_is_a_cassette_miss(tmp_path):
    gw, _ = gateway([ok(MATCH_OUT)], recorder=CassetteRecorder(tmp_path))
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    changed = MATCH_IN.model_copy(
        update={"candidates": [MATCH_IN.candidates[0].model_copy(update={"title": "Other"})]}
    )
    player = CassettePlayer.from_paths(tmp_path)
    with pytest.raises(CassetteMiss, match=r"candidates\[0\]\.title.*Other"):
        player.call(RoleTask.MATCH, changed, episode_id="ep")


def test_replay_of_a_different_task_or_an_extra_call_is_a_cassette_miss(tmp_path):
    gw, _ = gateway([ok(MATCH_OUT)], recorder=CassetteRecorder(tmp_path))
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    player = CassettePlayer.from_paths(tmp_path)
    with pytest.raises(CassetteMiss, match="recorded match"):
        player.call(RoleTask.PLAN_QUERIES, INPUTS[RoleTask.PLAN_QUERIES], episode_id="ep")
    player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    with pytest.raises(CassetteMiss, match="not on the cassette"):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    with pytest.raises(CassetteMiss, match="no cassette"):
        player.call(RoleTask.MATCH, MATCH_IN, episode_id="unknown")


def test_cassette_files_are_readable_json(tmp_path):
    gw, _ = gateway([ok(MATCH_OUT)], recorder=CassetteRecorder(tmp_path))
    gw.call(RoleTask.MATCH, MATCH_IN, episode_id="ep")
    text = (tmp_path / "ep.json").read_text()
    assert text.count("\n") > 20  # indented, one field per line
    entry = json.loads(text)["entries"][0]
    assert {"seq", "task", "slot", "role", "model", "effort", "input", "output", "usage"} <= set(
        entry
    )
    assert entry["input"]["trigger"]["headline"] == MATCH_IN.trigger.headline
