"""The beam search end to end, with scripted generator and critic records."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pmfa.config import Config, ForecastConfig
from pmfa.errors import ModelOutputError
from pmfa.forecast import (
    EvidenceBatch,
    ForecastEngine,
    SearchState,
    build_outcome_space,
    confidence,
    levels,
)
from pmfa.records.decision import SizingSummary
from pmfa.records.episode import Exit
from pmfa.records.forecast import NodeStatus, TreeNode
from pmfa.records.roles import (
    GenerateInput,
    ModelSlot,
    RejectionBrief,
    RoleTask,
    ScoreInput,
    ScoreOutput,
)

from .scripted import (
    AS_OF,
    C25,
    ScriptedModels,
    Spec,
    level,
    load_event,
    origin,
    passage,
    request,
    resolution,
    series,
    thought,
    threshold_size_fn,
)

FED = load_event("KXFEDDECISION-26DEC")
SPACE = build_outcome_space(FED, series("KXFEDDECISION"))
L1 = level("cpi:2026-11", 5, "November CPI release")
L2 = level("jobs:2026-11", 9, "November jobs report")
R = resolution(FED)

BASE = {"cut_25": 0.49, "hold": 0.41, "cut_gt25": 0.05, "hike_25": 0.05}
TAIL = {"cut_25": 1.0}


def three_level_world() -> ScriptedModels:
    """Root -> A, B, C (0.5, 0.3, 0.2) -> three children each (0.6, 0.3, 0.1) -> one leaf each.

    Every scenario says cut 25 has probability 0.49, except C3 (path mass 0.02), which
    says 1.0: without C3 the forecast sits below the 0.50 price, with it just above.
    """
    thoughts = {(L1.scheduled_event_id, None): [thought("A"), thought("B"), thought("C")]}
    specs = {"A": Spec(0.5, BASE), "B": Spec(0.3, BASE), "C": Spec(0.2, BASE)}
    for parent in "ABC":
        kids = [f"{parent}{i}" for i in (1, 2, 3)]
        thoughts[(L2.scheduled_event_id, parent)] = [thought(k) for k in kids]
        for kid, p in zip(kids, (0.6, 0.3, 0.1), strict=True):
            specs[kid] = Spec(p, BASE)
            thoughts[(R.scheduled_event_id, kid)] = [thought(f"{kid}-res", outcome_ids=("cut_25",))]
            specs[f"{kid}-res"] = Spec(1.0, BASE)
    specs["C3"] = Spec(0.1, TAIL)
    specs["C3-res"] = Spec(1.0, TAIL)
    return ScriptedModels(thoughts=thoughts, specs=specs)


def one_level_world(scenarios: dict[str, Spec], **thought_kw) -> ScriptedModels:
    thoughts = {
        (R.scheduled_event_id, None): [
            thought(c, outcome_ids=thought_kw.get(c, ("hold",))) for c in scenarios
        ]
    }
    return ScriptedModels(thoughts=thoughts, specs=dict(scenarios))


def engine(models, config: Config | None = None, **kw) -> ForecastEngine:
    kw.setdefault("size_fn", threshold_size_fn())
    return ForecastEngine(models, config or Config(), **kw)


def by_claim(nodes: list[TreeNode]) -> dict[str, TreeNode]:
    return {n.claim: n for n in nodes}


# --------------------------------------------------------------------------- the full tree


def test_protected_slot_expands_a_trade_flipping_scenario_and_freezes_one_that_changes_nothing():
    models = three_level_world()
    result = engine(models).run(request(FED, SPACE, [L1, L2, R]))
    nodes = by_claim(result.nodes)
    forecast = result.forecast
    assert result.exit is None and forecast is not None

    assert [nodes[c].status for c in "ABC"] == [NodeStatus.EXPANDED] * 3
    for claim in ("A1", "B1", "A2"):
        assert (nodes[claim].status, nodes[claim].reason) == (NodeStatus.EXPANDED, "beam")
    assert (nodes["C3"].status, nodes["C3"].reason) == (NodeStatus.EXPANDED, "protected")
    for claim in ("C1", "B2", "C2", "A3", "B3"):
        assert nodes[claim].status is NodeStatus.FROZEN
    assert nodes["C3-res"].status is NodeStatus.LEAF  # the protected scenario was expanded

    # both still count: the frozen B3 is a supporting path, and so is C3's leaf
    leaf_ids = {p.node_ids[-1] for p in forecast.paths}
    assert nodes["B3"].node_id in leaf_ids and nodes["C3-res"].node_id in leaf_ids
    assert forecast.coverage == pytest.approx(1.0)
    assert forecast.market_probabilities[C25] == pytest.approx(0.98 * 0.49 + 0.02)
    assert forecast.protected_node_id == nodes["C3"].node_id
    assert forecast.protected_drove_size  # YES at $5 versus NO at $5 is ten risk units of $1
    assert forecast.depth_reached == 3


@pytest.mark.parametrize("tradable", [True, False])
def test_a_scenario_whose_removal_zeroes_a_tradable_market_takes_the_protected_slot(tradable):
    models = three_level_world()
    models.specs["C3"] = models.specs["C3-res"] = Spec(0.1, BASE)
    only_b3 = {"cut_25": 0.49, "hold": 0.31, "cut_gt25": 0.05, "hike_25": 0.05, "hike_gt25": 0.1}
    models.specs["B3"] = Spec(0.1, only_b3)  # the only scenario with any mass on hike > 25
    h26 = "KXFEDDECISION-26DEC-H26"
    eng = engine(models, is_tradable=lambda ticker: tradable or ticker != h26)
    result = eng.run(request(FED, SPACE, [L1, L2, R]))
    nodes = by_claim(result.nodes)
    expected = NodeStatus.EXPANDED if tradable else NodeStatus.FROZEN
    assert nodes["B3"].status is expected
    assert nodes["C3"].status is NodeStatus.FROZEN
    assert result.forecast is not None
    assert result.forecast.protected_node_id == (nodes["B3"].node_id if tradable else None)
    assert not result.forecast.protected_drove_size


def test_a_full_tree_makes_at_most_nine_generator_and_nine_critic_calls():
    models = three_level_world()
    result = engine(models).run(request(FED, SPACE, [L1, L2, R]))
    n_gen, n_critic = len(models.tasks(RoleTask.GENERATE)), len(models.tasks(RoleTask.SCORE))
    assert n_gen <= 9 and n_critic <= 9
    assert n_gen == 1 + 3 + 4 and n_critic == 1 + 3 + 4  # root, beam of 3, beam of 3 + protected
    assert (result.generator_calls, result.critic_calls) == (n_gen, n_critic)
    slots = [s for _, s in models.tasks(RoleTask.SCORE)]
    assert slots[0] is ModelSlot.CRITIC_DEPTH1 and set(slots[1:]) == {ModelSlot.CRITIC_DEEP}
    assert {s for _, s in models.tasks(RoleTask.GENERATE)} == {ModelSlot.GENERATOR}


def test_the_critic_input_never_contains_the_generators_rationale():
    models = three_level_world()
    result = engine(models).run(request(FED, SPACE, [L1, L2, R]))
    assert all(n.rationale.startswith("GENERATOR-RATIONALE") for n in result.nodes)
    for payload, _ in models.tasks(RoleTask.SCORE):
        assert isinstance(payload, ScoreInput)
        assert "GENERATOR-RATIONALE" not in payload.model_dump_json()
        assert payload.scenarios and payload.passages


def test_search_state_survives_json_between_steps():
    eng = engine(three_level_world())
    state = eng.start(request(FED, SPACE, [L1, L2, R]))
    steps = 0
    while not eng.done(state):
        state = SearchState.model_validate_json(eng.step(state).model_dump_json())
        steps += 1
    assert steps == 3
    stepped = eng.finish(SearchState.model_validate_json(state.model_dump_json()))
    one_shot = engine(three_level_world()).run(request(FED, SPACE, [L1, L2, R]))
    assert stepped.forecast == one_shot.forecast
    assert stepped.nodes == one_shot.nodes


def test_on_node_sees_every_node_in_its_final_form():
    seen: dict[str, TreeNode] = {}
    result = engine(three_level_world(), on_node=lambda n: seen.__setitem__(n.node_id, n)).run(
        request(FED, SPACE, [L1, L2, R])
    )
    assert seen == {n.node_id: n for n in result.nodes}


# --------------------------------------------------------------------------- budget


def test_budget_exhaustion_aggregates_what_was_scored_so_far():
    models = three_level_world()
    models.max_calls = 5  # root gen + score, A's gen + score, B's gen; B's score is refused
    result = engine(models).run(request(FED, SPACE, [L1, L2, R]))
    forecast = result.forecast
    assert result.budget_exhausted and forecast is not None and forecast.budget_exhausted
    nodes = by_claim(result.nodes)
    members = {p.node_ids[-1] for p in forecast.paths}
    assert members == {nodes[c].node_id for c in ("A1", "A2", "A3", "B", "C")}
    assert nodes["B1"].status is NodeStatus.PROPOSED
    assert forecast.coverage == pytest.approx(1.0)
    assert forecast.confidence == pytest.approx(
        confidence(1.0, 1, full_origins=3, budget_exhausted=True)
    )
    assert forecast.confidence == pytest.approx(1.0 / 3 / 2)


def test_budget_exhausted_before_any_score_is_low_coverage():
    models = three_level_world()
    models.max_calls = 1
    result = engine(models).run(request(FED, SPACE, [L1, L2, R]))
    assert result.exit is Exit.LOW_COVERAGE and result.budget_exhausted


# --------------------------------------------------------------------------- coverage and exits


def test_coverage_is_the_total_mass_and_confidence_follows_the_formula():
    models = one_level_world(
        {"X": Spec(0.5, {"hold": 1.0}), "Y": Spec(0.3, {"cut_25": 1.0})}, Y=("cut_25",)
    )
    forecast = engine(models).run(request(FED, SPACE, [R])).forecast
    assert forecast is not None
    assert forecast.coverage == pytest.approx(0.8)
    assert forecast.market_probabilities["KXFEDDECISION-26DEC-H0"] == pytest.approx(0.625)
    assert forecast.market_probabilities[C25] == pytest.approx(0.375)
    assert forecast.n_independent_origins == 1
    assert forecast.confidence == pytest.approx(0.8 / 3)
    assert sum(forecast.outcome_probabilities.values()) == pytest.approx(1.0)


def test_low_coverage_exit():
    models = one_level_world({"X": Spec(0.3, {"hold": 1.0}), "Y": Spec(0.2, {"hold": 1.0})})
    result = engine(models).run(request(FED, SPACE, [R]))
    assert result.exit is Exit.LOW_COVERAGE and result.forecast is None
    assert "0.500" in result.detail


def test_rules_mismatch_when_every_first_level_scenario_fails_the_rules_check():
    thoughts = {
        (L1.scheduled_event_id, None): [
            thought("X", outcome_ids=("hold",)),
            thought("Y", outcome_ids=("cut_25",)),
        ]
    }
    models = ScriptedModels(thoughts=thoughts, specs={})
    result = engine(models).run(request(FED, SPACE, [L1, R]))
    assert result.exit is Exit.RULES_MISMATCH and result.forecast is None
    assert models.tasks(RoleTask.SCORE) == []
    for node in result.nodes:
        assert node.status is NodeStatus.DELETED
        assert any(c.name == "rules" and not c.passed for c in node.checks)


def test_each_check_deletes_a_scenario_and_records_why():
    thoughts = {
        (L1.scheduled_event_id, None): [
            thought("bad-rules", outcome_ids=("hold",)),
            thought("bad-evidence", passages=("p_late",)),
            thought("good"),
        ],
        (R.scheduled_event_id, "good"): [thought("leaf", outcome_ids=("hold",))],
    }
    specs = {"good": Spec(1.0, BASE), "leaf": Spec(1.0, BASE)}
    result = engine(ScriptedModels(thoughts=thoughts, specs=specs)).run(
        request(FED, SPACE, [L1, R])
    )
    nodes = by_claim(result.nodes)
    assert nodes["bad-rules"].status is NodeStatus.DELETED
    assert nodes["bad-rules"].reason == "failed check: rules"
    assert nodes["bad-evidence"].status is NodeStatus.DELETED
    assert nodes["bad-evidence"].reason == "failed check: evidence"
    assert nodes["good"].status is NodeStatus.EXPANDED
    assert result.forecast is not None

    late_level = level("after-close", 30)
    calendar = ScriptedModels(
        thoughts={(late_level.scheduled_event_id, None): [thought("whenever")]}, specs={}
    )
    result = engine(calendar).run(request(FED, SPACE, [late_level, R]))
    (node,) = result.nodes
    assert node.status is NodeStatus.DELETED and node.reason == "failed check: calendar"
    assert result.exit is Exit.LOW_COVERAGE


# --------------------------------------------------------------------------- critic validation


def test_sibling_probabilities_are_clipped_and_scaled_to_sum_to_one():
    models = one_level_world({"X": Spec(0.8, {"hold": 2.0, "cut_25": 2.0}), "Y": Spec(0.6, BASE)})
    nodes = by_claim(engine(models).run(request(FED, SPACE, [R])).nodes)
    assert nodes["X"].cond_probability == pytest.approx(0.8 / 1.4)
    assert nodes["Y"].cond_probability == pytest.approx(0.6 / 1.4)
    assert nodes["X"].outcome_probabilities["hold"] == pytest.approx(0.5)
    assert nodes["X"].outcome_probabilities["hike_gt25"] == 0.0
    assert sum(nodes["Y"].outcome_probabilities.values()) == pytest.approx(1.0)

    clipped = one_level_world({"X": Spec(1.4, BASE), "Y": Spec(-0.3, BASE)})
    nodes = by_claim(engine(clipped).run(request(FED, SPACE, [R])).nodes)
    assert (nodes["X"].cond_probability, nodes["Y"].cond_probability) == (1.0, 0.0)


def test_invalid_distributions_and_critic_rejections_are_reported_and_not_repeated():
    thoughts = {
        (L1.scheduled_event_id, None): [thought("X"), thought("W"), thought("V")],
        (R.scheduled_event_id, "X"): [thought("X-res", outcome_ids=("hold",))],
    }
    specs = {
        "X": Spec(0.9, BASE),
        "W": Spec(0.05, {"pause": 1.0}),
        "V": Spec(0.05, None, verdict="rejected", rejection_reason="ignores the rules"),
        "X-res": Spec(1.0, BASE),
    }
    models = ScriptedModels(thoughts=thoughts, specs=specs)
    reported: list[RejectionBrief] = []
    result = engine(models, on_rejection=reported.append).run(request(FED, SPACE, [L1, R]))
    nodes = by_claim(result.nodes)
    assert (nodes["W"].status, nodes["W"].reason) == (NodeStatus.REJECTED, "invalid distribution")
    assert (nodes["V"].status, nodes["V"].reason) == (NodeStatus.REJECTED, "ignores the rules")
    assert reported == [
        RejectionBrief(claim="W", reason="invalid distribution"),
        RejectionBrief(claim="V", reason="ignores the rules"),
    ]
    later = [p for p, _ in models.tasks(RoleTask.GENERATE)][1]
    assert isinstance(later, GenerateInput) and later.rejected == reported


def test_a_missing_distribution_is_rejected():
    models = one_level_world({"X": Spec(0.9, BASE), "Y": Spec(0.1, None)})
    nodes = by_claim(engine(models).run(request(FED, SPACE, [R])).nodes)
    assert (nodes["Y"].status, nodes["Y"].reason) == (NodeStatus.REJECTED, "invalid distribution")


def test_a_critic_answer_without_exactly_one_score_per_scenario_is_a_model_error():
    models = one_level_world({"X": Spec(0.5, BASE), "Y": Spec(0.5, BASE)})
    models.score_hook = lambda _payload, out: ScoreOutput(scores=out.scores[:1])
    with pytest.raises(ModelOutputError):
        engine(models).run(request(FED, SPACE, [R]))


# --------------------------------------------------------------------------- evidence requests


def _followup():
    calls: list[str] = []
    extra = passage("p9", "o9")

    def more(query: str) -> EvidenceBatch:
        calls.append(query)
        return EvidenceBatch(passages=[extra], origins=[origin("o9", primary=True)])

    return calls, more


def test_an_evidence_request_is_honoured_once_and_the_siblings_are_rescored_once():
    models = one_level_world(
        {"X": Spec(0.5, BASE, evidence_request="more on X"), "Y": Spec(0.3, BASE)}
    )
    models.rescore_specs = {"X": Spec(0.7, BASE, evidence_request="still more on X")}
    calls, more = _followup()
    result = engine(models, more_evidence=more).run(request(FED, SPACE, [R]))
    assert calls == ["more on X"]
    scores = [p for p, _ in models.tasks(RoleTask.SCORE)]
    assert len(scores) == 2
    assert "p9" in [p.passage_id for p in scores[1].passages]
    nodes = by_claim(result.nodes)
    assert nodes["X"].evidence_requested and nodes["X"].cond_probability == pytest.approx(0.7)


def test_evidence_followups_are_capped_per_episode():
    models = one_level_world(
        {
            "X": Spec(0.5, BASE, evidence_request="more on X"),
            "Y": Spec(0.3, BASE, evidence_request="more on Y"),
        }
    )
    calls, more = _followup()
    config = Config(forecast=ForecastConfig(max_evidence_followups=1))
    engine(models, config, more_evidence=more).run(request(FED, SPACE, [R]))
    assert calls == ["more on X"]


# --------------------------------------------------------------------------- fast path and ladders


def test_an_event_closing_within_the_hour_uses_one_level_on_the_fast_path_slot():
    as_of = FED.close_time - timedelta(minutes=30)
    tree_levels = levels(FED, [], as_of, Config())
    models = one_level_world({"X": Spec(0.9, {"hold": 1.0})})
    result = engine(models).run(
        request(FED, SPACE, [L1, *tree_levels], fast_path=True, as_of=as_of)
    )
    assert result.forecast is not None and result.forecast.fast_path
    assert result.forecast.depth_reached == 1
    assert {s for _, _, s in models.calls} == {ModelSlot.FAST_PATH}
    assert len(models.calls) == 2


def test_ladder_forecast_probabilities_are_monotone_across_strikes():
    cpi = load_event("KXCPIYOY-26NOV")
    space = build_outcome_space(cpi, series("KXCPIYOY"))
    res = resolution(cpi)
    thoughts = {
        (res.scheduled_event_id, None): [
            thought("hot", low=3.5),
            thought("cool", high=3.5),
        ]
    }
    specs = {
        "hot": Spec(0.6, quantiles=(3.4, 3.5, 3.6, 3.7, 3.9)),
        "cool": Spec(0.4, quantiles=(3.1, 3.3, 3.4, 3.5, 3.6)),
    }
    result = engine(
        ScriptedModels(thoughts=thoughts, specs=specs), size_fn=lambda _f: SizingSummary()
    ).run(request(cpi, space, [res]))
    forecast = result.forecast
    assert forecast is not None
    probs = [
        forecast.market_probabilities[f"KXCPIYOY-26NOV-T{s}"]
        for s in ("3.4", "3.5", "3.6", "3.7", "3.8")
    ]
    assert probs == sorted(probs, reverse=True)
    assert all(0.0 < p < 1.0 for p in probs)
    nodes = by_claim(result.nodes)
    assert nodes["hot"].quantiles is not None


def test_confidence_formula():
    assert confidence(0.9, 6, full_origins=3, budget_exhausted=False) == pytest.approx(0.9)
    assert confidence(0.9, 1, full_origins=3, budget_exhausted=False) == pytest.approx(0.3)
    assert confidence(0.9, 1, full_origins=3, budget_exhausted=True) == pytest.approx(0.15)


def test_as_of_is_respected_in_every_prompt():
    models = three_level_world()
    engine(models).run(request(FED, SPACE, [L1, L2, R]))
    for _, payload, _ in models.calls:
        for p in payload.passages:  # type: ignore[attr-defined]
            assert p.published_at <= AS_OF
