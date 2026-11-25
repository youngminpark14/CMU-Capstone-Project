"""Seam 1: whole episodes against small recorded worlds with scripted roles.

Each test is named after a scenario in the spec's "Testing Decisions" and
asserts what the episode decided: the exit, the forecast, the proposal, the
routing tier, and the orders.
"""

from __future__ import annotations

import dataclasses
import itertools
from datetime import timedelta
from decimal import Decimal

import pytest

from pmfa.episode import EpisodeController, episode_id_for
from pmfa.errors import BudgetExhausted, ModelOutputError, RunBudgetExhausted
from pmfa.records.base import RunMode, Stage
from pmfa.records.decision import (
    OrderResult,
    OrderStatus,
    ProposalStatus,
    ReasonCode,
    Review,
    ReviewDecision,
    ReviewPolicy,
    RoutingTier,
)
from pmfa.records.episode import EpisodeStatus, Exit
from pmfa.records.evidence import Trigger, TriggerKind
from pmfa.records.forecast import NodeStatus
from pmfa.records.market import Side
from pmfa.records.roles import MatchInput, ModelSlot, PassageSelectionInput, RoleTask

from .support import (
    AS_OF,
    C25,
    CANARY,
    CPI,
    CPI_STRIKES,
    DEFAULT_PRICES,
    FED,
    FED_CLOSE,
    FED_JAN,
    FUTURE_CANARY,
    H0,
    HAWKISH,
    JOBS_DEC,
    MINUTES,
    NEWS,
    PHANTOM_PRICES,
    RESOLUTION,
    SPEECH,
    Script,
    Spec,
    T,
    advance,
    books_for,
    calibrated_ledger,
    check_trace,
    cpi_file,
    doc_trigger,
    fed_event,
    fed_file,
    hawkish_script,
    make_world,
    no_book,
    run_episode,
    syndicated,
    with_transport,
)


def orders(world) -> list:
    return world.store.list_orders()


# --------------------------------------------------------------------------- 1


def test_01_no_matching_event_exits_no_match(transport):
    record, world = run_episode(fed_file(), Script(matches=[None]), transport=transport)
    assert record.exit is Exit.NO_MATCH
    assert record.forecast is None and record.proposal is None and orders(world) == []


def test_01_a_ticker_that_was_not_a_candidate_is_no_match():
    record, world = run_episode(fed_file(), Script(matches=["KXFEDDECISION-99XXX"]))
    assert record.exit is Exit.NO_MATCH
    assert "not a candidate" in record.exit_detail
    assert orders(world) == []


# --------------------------------------------------------------------------- 2


def test_02_one_secondary_origin_syndicated_twice_is_insufficient_origins():
    copies = [syndicated(NEWS, 1, "wire.example"), syndicated(NEWS, 2, "paper.example")]
    file = fed_file(
        documents=[NEWS, *copies], trigger=doc_trigger(NEWS, at=AS_OF + timedelta(hours=1))
    )
    record, world = run_episode(file, hawkish_script())
    assert record.exit is Exit.INSUFFICIENT_ORIGINS
    assert record.forecast is None and orders(world) == []


# --------------------------------------------------------------------------- 3


def test_03_the_market_already_prices_the_news_is_no_edge():
    event = fed_event(prices=PHANTOM_PRICES)
    record, world = run_episode(
        fed_file(event=event, order_books=books_for(event)), hawkish_script()
    )
    assert record.exit is Exit.NO_EDGE
    assert record.forecast is not None
    assert record.forecast.market_probabilities[C25] == pytest.approx(HAWKISH["cut_25"])
    assert record.proposal is None and orders(world) == []
    # The forecast is still recorded against the price at the as-of time.
    ledger = {e.market_ticker: e for e in world.store.ledger()}
    assert ledger[C25].market_price == pytest.approx(0.18) and not ledger[C25].traded


# --------------------------------------------------------------------------- 4


def test_04_hawkish_fed_minutes_buy_a_small_no_position(transport):
    record, world = run_episode(fed_file(), hawkish_script(), transport=transport)
    assert record.exit is Exit.EXECUTED
    assert record.forecast is not None
    assert record.forecast.market_probabilities[C25] == pytest.approx(0.18)
    p = record.proposal
    assert p is not None and (p.market_ticker, p.side) == (C25, Side.NO)
    assert 0 < p.contracts <= 25
    assert p.cost <= Decimal("0.05") * p.bankroll  # within the per-market cap
    assert p.limit_price <= p.edge_bound_price
    [order] = orders(world)
    assert order.status is OrderStatus.FILLED and order.filled_contracts == p.contracts
    [position] = world.executor.positions(FED)
    assert (position.market_ticker, position.side, position.contracts) == (
        C25,
        Side.NO,
        p.contracts,
    )
    assert record.routing is not None and record.routing.tier is RoutingTier.APPROVE
    entries = {e.market_ticker: e for e in world.store.ledger()}
    assert set(entries) == {m.ticker for m in fed_event().markets}
    assert entries[C25].traded and entries[C25].tier == "approve"
    assert not entries[H0].traded and entries[H0].tier == ""
    # Every cited document's content hash is remembered (a later change is a halt condition).
    assert world.store.document_hash(MINUTES.doc_id) == MINUTES.content_hash


# --------------------------------------------------------------------------- 5

BAD_RULES = {
    (RESOLUTION, None): [
        T("The Committee pauses", ("pause",)),
        T("The Committee does something else", ("other",)),
    ]
}


def test_05_every_first_level_scenario_failing_the_rules_rematches_once_then_rules_mismatch(
    transport,
):
    jan = fed_event(FED_JAN, close=FED_CLOSE + timedelta(days=50))
    dec = fed_event()
    file = fed_file(
        events=[dec, jan], order_books={**books_for(dec), **books_for(jan)}, scheduled_events=[]
    )
    script = Script(matches=[FED, FED_JAN], tree=BAD_RULES)
    record, world = run_episode(file, script, transport=transport)
    assert record.exit is Exit.RULES_MISMATCH
    assert "re-match" in record.exit_detail
    models = world.models
    matches = models.calls_for(RoleTask.MATCH)
    assert len(matches) == 2  # one re-match, no more
    second = matches[1].payload
    assert isinstance(second, MatchInput)
    assert FED not in {c.event_ticker for c in second.candidates}
    assert record.event_ticker == FED_JAN
    assert record.forecast is None and orders(world) == []


def test_05_a_trigger_that_named_its_event_cannot_rematch():
    trigger = Trigger(trigger_id="sched-1", kind=TriggerKind.SCHEDULE, at=AS_OF, event_ticker=FED)
    record, world = run_episode(fed_file(trigger=trigger), Script(matches=[], tree=BAD_RULES))
    assert record.exit is Exit.RULES_MISMATCH
    assert world.models.calls_for(RoleTask.MATCH) == []


# --------------------------------------------------------------------------- 6

A_DIST = {"hold": 0.88, "cut_25": 0.06, "hike_25": 0.04, "cut_gt25": 0.01, "hike_gt25": 0.01}
B_DIST = {"hold": 0.02, "cut_25": 0.95, "hike_25": 0.01, "cut_gt25": 0.01, "hike_gt25": 0.01}
FLIP_PRICES = {
    **DEFAULT_PRICES,
    "H0": ("0.63", "0.65"),
    "C25": ("0.19", "0.21"),
}


def two_level_script(**changes) -> Script:
    """Jobs report, then resolution. Beam width 1: A is expanded; B (removing it changes the
    trade) takes the protected slot; C (removing it changes nothing) is frozen."""
    jobs = JOBS_DEC.scheduled_event_id
    script = Script(
        tree={
            (jobs, None): [
                T("Payrolls come in strong"),
                T("Payrolls collapse"),
                T("Payrolls are mixed"),
            ],
            (RESOLUTION, "Payrolls come in strong"): [T("Hold after strong jobs", ("hold",))],
            (RESOLUTION, "Payrolls collapse"): [T("Cut after weak jobs", ("cut_25",))],
            (RESOLUTION, "Payrolls are mixed"): [T("Hold after mixed jobs", ("hold",))],
        },
        scores={
            "Payrolls come in strong": Spec(0.58, A_DIST),
            "Payrolls collapse": Spec(0.27, B_DIST),
            "Payrolls are mixed": Spec(0.15, A_DIST),
            "Hold after strong jobs": Spec(1.0, A_DIST),
            "Cut after weak jobs": Spec(1.0, B_DIST),
            "Hold after mixed jobs": Spec(1.0, A_DIST),
        },
    )
    for key, value in changes.items():
        setattr(script, key, value)
    return script


def two_level_file(**changes):
    event = fed_event(prices=FLIP_PRICES)
    base = {
        "event": event,
        "order_books": books_for(event, depth="5000"),  # $500 resting even at 0.21
        "scheduled_events": [JOBS_DEC],
        "config_overrides": {"forecast": {"beam_width": 1}},
    }
    base.update(changes)
    return fed_file(**base)


def test_06_a_trade_changing_tail_scenario_takes_the_protected_slot_and_one_that_changes_nothing_is_frozen(
    transport,
):
    record, world = run_episode(two_level_file(), two_level_script(), transport=transport)
    forecast = record.forecast
    assert forecast is not None
    nodes = {n.claim: n for n in world.store.get_nodes(record.episode_id)}
    strong, collapse, mixed = (
        nodes["Payrolls come in strong"],
        nodes["Payrolls collapse"],
        nodes["Payrolls are mixed"],
    )
    assert (strong.status, strong.reason) == (NodeStatus.EXPANDED, "beam")
    assert (collapse.status, collapse.reason) == (NodeStatus.EXPANDED, "protected")
    assert mixed.status is NodeStatus.FROZEN
    assert nodes["Cut after weak jobs"].status is NodeStatus.LEAF  # the protected one was expanded
    assert "Hold after mixed jobs" not in nodes  # the frozen one was not
    # Both still count in the forecast, with their probability.
    leaves = {path.node_ids[-1] for path in forecast.paths}
    assert nodes["Cut after weak jobs"].node_id in leaves and mixed.node_id in leaves
    assert forecast.coverage == pytest.approx(1.0)
    expected_cut = 0.58 * A_DIST["cut_25"] + 0.27 * B_DIST["cut_25"] + 0.15 * A_DIST["cut_25"]
    assert forecast.market_probabilities[C25] == pytest.approx(expected_cut)
    assert forecast.protected_node_id == collapse.node_id and forecast.protected_drove_size
    # The protected scenario drove the size, so the trade is never auto-routed.
    assert record.routing is not None and "protected_not_driving" in record.routing.reasons
    # The critic's slot: the depth-1 critic at the first level, the deep critic below.
    slots = [c.slot for c in world.models.calls_for(RoleTask.SCORE)]
    assert slots[0] is ModelSlot.CRITIC_DEPTH1
    assert set(slots[1:]) == {ModelSlot.CRITIC_DEEP}


# --------------------------------------------------------------------------- 7


def test_07_a_proposal_over_a_cap_is_gate_rejected_with_no_retry(monkeypatch):
    """The gate is the independent check: sizing (patched here) hands over 80 contracts,
    over the 25-contract order cap and, at $55.62, over the $50 per-market cap."""
    import pmfa.episode.pipeline as pipeline
    from pmfa.decision import fee

    real = pipeline.size_event
    sized: list[int] = []

    def oversized(inputs, config):
        decision = real(inputs, config)
        assert decision.proposal is not None
        p = decision.proposal
        n = 80
        order_fee = fee(n, p.limit_price, inputs.series.fee_type, inputs.series.fee_multiplier)
        big = p.model_validate(
            {
                **p.model_dump(),
                "contracts": n,
                "fee": order_fee,
                "cost": n * p.limit_price + order_fee,
            }
        )
        sized.append(n)
        return decision.model_copy(update={"proposal": big})

    monkeypatch.setattr(pipeline, "size_event", oversized)
    record, world = run_episode(fed_file(), hawkish_script())
    assert record.exit is Exit.GATE_REJECTED
    assert record.gate is not None
    assert {"contracts_per_order", "dollars_per_market"} <= set(record.gate.reasons)
    assert (
        "contracts_per_order" in record.exit_detail and "dollars_per_market" in record.exit_detail
    )
    assert orders(world) == []
    [(proposal, status)] = world.store.list_proposals()
    assert status is ProposalStatus.CANCELLED and proposal.contracts == 80
    assert sized == [80]  # sized once: never retried, at any size


def stopped_world(points):
    world = make_world(fed_file(), hawkish_script())
    for at, equity in points:
        world.store.record_equity(at, Decimal(equity))
    return world


def second_trigger():
    later = AS_OF + timedelta(minutes=5)
    return Trigger(
        trigger_id="sched-after-stop", kind=TriggerKind.SCHEDULE, at=later, event_ticker=FED
    )


@pytest.mark.parametrize(
    ("points", "reason"),
    [
        # Equity stood at 1,050 earlier today and is 1,000 now: past the 3% daily loss stop.
        ([(AS_OF - timedelta(hours=5), "1050")], "daily loss stop"),
        # A peak of 1,200 three days ago, 1,000 since yesterday: a 16.7% drawdown.
        (
            [(AS_OF - timedelta(days=3), "1200"), (AS_OF - timedelta(days=1), "1000")],
            "drawdown",
        ),
    ],
)
def test_a_tripped_stop_halts_persists_the_flag_and_refuses_the_next_episode(points, reason):
    world = stopped_world(points)
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
        check_trace(record)
        assert record.exit is Exit.HALTED and reason in record.exit_detail
        halt = world.store.get_halt()
        assert halt.halted and reason in halt.reason
        assert orders(world) == []
        world.clock.set(AS_OF + timedelta(minutes=5))  # type: ignore[attr-defined]
        nxt = controller.run(second_trigger())
    check_trace(nxt)
    assert nxt.exit is Exit.HALTED
    assert orders(world) == []
    assert world.store.get_halt().reason == halt.reason  # the original reason is kept


# --------------------------------------------------------------------------- 8


def test_08_the_price_moves_past_the_edge_before_execution_and_no_order_is_sent(transport):
    # Buying NO at 0.68 now meets only 0.73 (YES bid 0.27): past the 0.71 edge bound.
    file = fed_file(execution_books={C25: no_book(C25, [("0.27", "1000")])})
    record, world = run_episode(file, hawkish_script(), transport=transport)
    assert orders(world) == []
    # The episode recomputed once with the fresh book, and the edge is gone.
    assert record.exit is Exit.NO_EDGE
    assert sum(1 for s in record.trace if s.node == "size" and s.kind == "action") == 3


def test_08_after_approval_a_price_above_the_limit_exits_stale_price():
    # NO now costs 0.69: above the 0.68 limit, still inside the 0.71 edge bound.
    file = fed_file(execution_books={C25: no_book(C25, [("0.31", "1000")])})
    world = make_world(file, hawkish_script(), policy=ReviewPolicy.PAUSE)
    with EpisodeController(world) as controller:
        paused = controller.run(file.trigger)
        assert paused.status is EpisodeStatus.PENDING_REVIEW
        advance(world, 60)
        record = controller.resume(paused.episode_id, approve(paused, world))
    check_trace(record)
    assert record.exit is Exit.STALE_PRICE
    assert orders(world) == []


# --------------------------------------------------------------------------- 9


def approve(paused, world) -> Review:
    return Review(
        proposal_id=paused.proposal.proposal_id,
        decision=ReviewDecision.APPROVE,
        decided_at=world.clock.now(),
    )


def paused_episode(file=None, transport: str = "direct"):
    file = with_transport(file or fed_file(), transport)
    world = make_world(file, hawkish_script(), policy=ReviewPolicy.PAUSE)
    controller = EpisodeController(world)
    paused = controller.run(file.trigger)
    check_trace(paused)
    return controller, world, paused


def test_09_approve_tier_pauses_with_the_proposal_and_its_card_stored(transport):
    controller, world, paused = paused_episode(transport=transport)
    with controller:
        assert paused.status is EpisodeStatus.PENDING_REVIEW and paused.exit is None
        assert paused.routing is not None and paused.routing.tier is RoutingTier.APPROVE
        stored = world.store.get_proposal(paused.proposal.proposal_id)
        assert stored is not None
        proposal, status, card = stored
        assert status is ProposalStatus.PENDING and proposal == paused.proposal
        assert card is not None and card.market_ticker == C25 and card.passages
        assert orders(world) == []


def test_09_approve_after_the_pause_executes_the_proposal(transport):
    controller, world, paused = paused_episode(transport=transport)
    with controller:
        advance(world, 60)
        record = controller.resume(paused.episode_id, approve(paused, world))
    check_trace(record)
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    assert record.review is not None and record.review.decision is ReviewDecision.APPROVE
    [order] = orders(world)
    assert order.requested_contracts == paused.proposal.contracts
    assert world.store.get_proposal(paused.proposal.proposal_id)[1] is ProposalStatus.EXECUTED
    entry = next(e for e in world.store.ledger() if e.market_ticker == C25)
    assert entry.traded and entry.override == ""


def test_09_resize_after_the_pause_executes_fewer_contracts():
    controller, world, paused = paused_episode()
    with controller:
        advance(world, 60)
        review = Review(
            proposal_id=paused.proposal.proposal_id,
            decision=ReviewDecision.RESIZE,
            contracts=5,
            reason_code=ReasonCode.SIZE_TOO_LARGE,
            decided_at=world.clock.now(),
        )
        record = controller.resume(paused.episode_id, review)
    assert record.exit is Exit.EXECUTED
    p = record.proposal
    assert p is not None and p.contracts == 5 and p.proposal_id == paused.proposal.proposal_id
    assert p.cost < paused.proposal.cost and p.fee <= paused.proposal.fee
    assert p.cost == 5 * p.limit_price + p.fee
    [order] = orders(world)
    assert order.requested_contracts == 5 and order.filled_contracts == 5
    entry = next(e for e in world.store.ledger() if e.market_ticker == C25)
    assert entry.override == "resize:size_too_large"


def test_09_a_resize_can_only_shrink_and_an_invalid_review_leaves_the_episode_paused():
    controller, world, paused = paused_episode()
    with controller:
        grow = Review(
            proposal_id=paused.proposal.proposal_id,
            decision=ReviewDecision.RESIZE,
            contracts=paused.proposal.contracts + 5,
            decided_at=world.clock.now(),
        )
        with pytest.raises(ValueError):
            controller.resume(paused.episode_id, grow)
        assert world.store.get_episode(paused.episode_id).status is EpisodeStatus.PENDING_REVIEW
        record = controller.resume(paused.episode_id, approve(paused, world))
    assert record.exit is Exit.EXECUTED


def test_09_reject_after_the_pause_records_the_reason_and_sends_nothing():
    controller, world, paused = paused_episode()
    with controller:
        review = Review(
            proposal_id=paused.proposal.proposal_id,
            decision=ReviewDecision.REJECT,
            reason_code=ReasonCode.DISAGREE_WITH_FORECAST,
            decided_at=world.clock.now(),
        )
        record = controller.resume(paused.episode_id, review)
    check_trace(record)
    assert record.exit is Exit.REJECTED_BY_HUMAN
    assert (
        record.review is not None and record.review.reason_code is ReasonCode.DISAGREE_WITH_FORECAST
    )
    assert orders(world) == []
    assert world.store.get_proposal(paused.proposal.proposal_id)[1] is ProposalStatus.REJECTED
    entry = next(e for e in world.store.ledger() if e.market_ticker == C25)
    assert entry.override == "reject:disagree_with_forecast" and not entry.traded


def test_09_a_proposal_expired_by_time_never_executes():
    controller, world, paused = paused_episode()
    with controller:
        advance(world, 11 * 60)  # past the ten-minute expiry
        record = controller.resume(paused.episode_id, approve(paused, world))
    check_trace(record)
    assert record.exit is Exit.EXPIRED
    assert orders(world) == []
    assert world.store.get_proposal(paused.proposal.proposal_id)[1] is ProposalStatus.EXPIRED


def test_09_a_proposal_expired_by_price_never_executes():
    file = fed_file(execution_books={C25: no_book(C25, [("0.27", "1000")])})
    controller, world, paused = paused_episode(file)
    with controller:
        advance(world, 60)
        record = controller.resume(paused.episode_id, approve(paused, world))
    assert record.exit is Exit.EXPIRED and "edge bound" in record.exit_detail
    assert orders(world) == []


def test_09_expire_ends_a_paused_episode_without_an_order():
    controller, world, paused = paused_episode()
    with controller:
        record = controller.expire(paused.episode_id)
        again = controller.resume(paused.episode_id, approve(paused, world))
    assert record.exit is Exit.EXPIRED and again.exit is Exit.EXPIRED
    assert orders(world) == []


# --------------------------------------------------------------------------- 10


@pytest.mark.parametrize(
    ("mode", "policy"),
    list(itertools.product(RunMode, ReviewPolicy)),
)
def test_10_halt_flag_set_means_no_order_in_any_mode(mode, policy):
    record, world = run_episode(fed_file(halted=True), hawkish_script(), mode=mode, policy=policy)
    assert record.exit is Exit.HALTED
    assert record.gate is not None and "halt" in record.gate.reasons
    assert orders(world) == []


def test_10_a_halt_set_during_the_review_blocks_the_approved_order(transport):
    controller, world, paused = paused_episode(transport=transport)
    with controller:
        world.store.set_halt("operator halt", world.clock.now())
        record = controller.resume(paused.episode_id, approve(paused, world))
    assert record.exit is Exit.HALTED
    assert orders(world) == []


# --------------------------------------------------------------------------- 11


def test_11_call_budget_exhausted_forecasts_from_the_current_beam_with_reduced_confidence():
    # match, plan, select, generate and score level 1, generate level 2: the next call is over.
    file = two_level_file(
        config_overrides={"forecast": {"beam_width": 1}, "budget": {"max_calls_per_episode": 6}}
    )
    record, _world = run_episode(file, two_level_script())
    forecast = record.forecast
    assert forecast is not None and forecast.budget_exhausted
    assert forecast.depth_reached == 1
    full = forecast.coverage * min(1.0, forecast.n_independent_origins / 3)
    assert forecast.confidence == pytest.approx(full / 2)
    assert record.exit is not Exit.ERROR
    assert record.usage.calls == 6


# --------------------------------------------------------------------------- 12


def test_12_an_event_closing_within_the_hour_takes_the_depth_1_fast_path():
    at = FED_CLOSE - timedelta(minutes=30)
    file = fed_file(trigger=doc_trigger(MINUTES, at=at), scheduled_events=[JOBS_DEC])
    record, world = run_episode(file, hawkish_script())
    forecast = record.forecast
    assert forecast is not None and forecast.fast_path and forecast.depth_reached == 1
    slots = {c.slot for c in world.models.calls if c.task in (RoleTask.GENERATE, RoleTask.SCORE)}
    assert slots == {ModelSlot.FAST_PATH}


# --------------------------------------------------------------------------- 13


def test_13_a_canary_document_dated_after_the_as_of_time_is_never_retrieved(transport):
    script = hawkish_script()
    script.plan = script.plan.model_copy(
        update={"query_for": f"{CANARY} federal funds rate steady December"}
    )
    record, world = run_episode(fed_file(), script, transport=transport)
    offered = [
        p
        for call in world.models.calls_for(RoleTask.SELECT_PASSAGES)
        for p in call.payload.passages  # type: ignore[attr-defined]
    ]
    assert offered and all(CANARY not in p.text for p in offered)
    assert all(not pid.startswith(FUTURE_CANARY.doc_id) for pid in record.passage_ids)
    assert record.forecast is not None
    assert all(not pid.startswith(FUTURE_CANARY.doc_id) for pid in record.forecast.passage_ids)


# --------------------------------------------------------------------------- 14


def test_14_a_partial_fill_records_the_filled_part_and_the_remainder_is_gone():
    # Only 8 contracts are offered at the 0.68 limit when the order arrives.
    exec_book = no_book(C25, [("0.32", "8"), ("0.25", "1000")])
    record, world = run_episode(fed_file(execution_books={C25: exec_book}), hawkish_script())
    assert record.exit is Exit.EXECUTED
    [order] = orders(world)
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_contracts == 8 < order.requested_contracts
    assert record.orders == [order]
    [position] = world.executor.positions(FED)
    assert position.contracts == 8 and position.side is Side.NO
    assert world.executor.cancel_all() == 0  # nothing rests


# --------------------------------------------------------------------------- 15


@pytest.mark.parametrize("task", [RoleTask.MATCH, RoleTask.PLAN_QUERIES, RoleTask.SCORE])
def test_15_malformed_model_output_exits_error_with_no_order(task):
    script = hawkish_script(fail={task: ModelOutputError("invalid twice")})
    record, world = run_episode(fed_file(), script)
    assert record.exit is Exit.ERROR and "model output error" in record.exit_detail
    assert record.proposal is None and orders(world) == []


# --------------------------------------------------------------------------- 16


def test_16_a_ladder_event_gets_per_market_probabilities_monotone_across_strikes():
    claims = {
        "Inflation stays near 3.6%": ((3.5, 3.7), (3.35, 3.5, 3.6, 3.7, 3.85), 0.6),
        "Inflation cools below 3.5%": ((None, 3.5), (3.2, 3.3, 3.4, 3.5, 3.6), 0.25),
        "Inflation runs above 3.7%": ((3.7, None), (3.6, 3.7, 3.8, 3.9, 4.1), 0.15),
    }
    script = Script(
        matches=[CPI],
        tree={
            (RESOLUTION, None): [T(c, low=lo, high=hi) for c, ((lo, hi), _, _) in claims.items()]
        },
        scores={c: Spec(p, quantiles=q) for c, (_, q, p) in claims.items()},
    )
    record, _world = run_episode(cpi_file(), script)
    forecast = record.forecast
    assert forecast is not None and record.event_ticker == CPI
    probs = [forecast.market_probabilities[f"{CPI}-T{s}"] for s in CPI_STRIKES]
    assert all(a >= b for a, b in itertools.pairwise(probs))
    assert probs[0] > probs[-1]


# --------------------------------------------------------------------------- routing and policies


def autonomous_file(**changes):
    base = {
        "stage": Stage.AUTONOMOUS,
        "ledger": calibrated_ledger(),
        "config_overrides": {"sizing": {"kelly_fraction": "0.1"}},
    }
    base.update(changes)
    return fed_file(**base)


def test_auto_executes_without_a_pause_at_the_autonomous_stage(transport):
    record, world = run_episode(
        autonomous_file(), hawkish_script(), policy=ReviewPolicy.PAUSE, transport=transport
    )
    assert record.routing is not None and record.routing.tier is RoutingTier.AUTO
    assert record.exit is Exit.EXECUTED and record.review is None
    assert not any(s.node in ("pause", "review") for s in record.trace)
    assert len(orders(world)) == 1


def test_a_would_be_auto_trade_below_the_autonomous_stage_goes_to_review_and_is_logged():
    file = autonomous_file(stage=Stage.APPROVAL)
    record, world = run_episode(file, hawkish_script(), policy=ReviewPolicy.PAUSE)
    assert record.status is EpisodeStatus.PENDING_REVIEW
    routing = record.routing
    assert routing is not None and routing.tier is RoutingTier.APPROVE and routing.would_be_auto
    assert any("would be auto" in s.summary for s in record.trace if s.node == "route")
    assert orders(world) == []


def test_fill_tagged_fills_an_approve_tier_proposal_and_tags_it():
    record, world = run_episode(fed_file(), hawkish_script(), policy=ReviewPolicy.FILL_TAGGED)
    assert record.routing is not None and record.routing.tier is RoutingTier.APPROVE
    assert record.exit is Exit.EXECUTED and record.fill_tagged
    assert any("fill_tagged" in s.summary for s in record.trace if s.node == "route")
    assert record.review is None
    assert len(orders(world)) == 1


def test_a_price_move_trigger_with_no_new_evidence_exits_no_evidence():
    event = fed_event(prices=PHANTOM_PRICES)
    world = make_world(fed_file(event=event, order_books=books_for(event)), hawkish_script())
    with EpisodeController(world) as controller:
        first = controller.run(world.file.trigger)
        assert first.exit is Exit.NO_EDGE  # a forecast was made at the as-of time
        later = AS_OF + timedelta(hours=1)
        world.clock.set(later)  # type: ignore[attr-defined]
        move = Trigger(trigger_id="move-1", kind=TriggerKind.PRICE_MOVE, at=later, event_ticker=FED)
        record = controller.run(move)
    check_trace(record)
    assert record.exit is Exit.NO_EVIDENCE and "newer than the last forecast" in record.exit_detail
    assert len(world.models.calls_for(RoleTask.MATCH)) == 1  # the price move named its event


def test_a_price_move_with_new_evidence_is_forecast():
    later = AS_OF + timedelta(hours=1)
    fresh = SPEECH.model_copy(
        update={"doc_id": "speech-fresh", "published_at": later - timedelta(minutes=5)}
    )
    event = fed_event(prices=PHANTOM_PRICES)
    file = fed_file(event=event, order_books=books_for(event), documents=[MINUTES, SPEECH, fresh])
    world = make_world(file, hawkish_script())
    with EpisodeController(world) as controller:
        controller.run(file.trigger)
        world.clock.set(later)  # type: ignore[attr-defined]
        move = Trigger(trigger_id="move-2", kind=TriggerKind.PRICE_MOVE, at=later, event_ticker=FED)
        record = controller.run(move)
    assert record.forecast is not None
    assert record.passage_ids and all(pid.startswith("speech-fresh") for pid in record.passage_ids)


def test_every_exit_writes_exactly_one_episode_record():
    cases = {
        Exit.NO_MATCH: (fed_file(), Script(matches=[None])),
        Exit.NO_EDGE: (
            fed_file(
                event=fed_event(prices=PHANTOM_PRICES),
                order_books=books_for(fed_event(prices=PHANTOM_PRICES)),
            ),
            hawkish_script(),
        ),
        Exit.EXECUTED: (fed_file(), hawkish_script()),
        Exit.HALTED: (fed_file(halted=True), hawkish_script()),
        Exit.ERROR: (fed_file(), hawkish_script(fail={RoleTask.GENERATE: ModelOutputError("x")})),
    }
    for expected, (file, script) in cases.items():
        record, world = run_episode(file, script)
        assert record.exit is expected
        [stored] = world.store.list_episodes()
        assert stored == record and stored.status is EpisodeStatus.DONE
        assert stored.finished_at is not None


def test_passages_the_researcher_did_not_offer_are_ignored():
    script = hawkish_script(
        keep=lambda payload: ["made-up-passage", payload.passages[0].passage_id]
    )
    record, _ = run_episode(fed_file(), script)
    assert "made-up-passage" not in record.passage_ids
    assert len(record.passage_ids) == 1


def test_no_kept_passage_exits_no_evidence():
    record, world = run_episode(fed_file(), hawkish_script(keep=lambda payload: []))
    assert record.exit is Exit.NO_EVIDENCE
    selections = world.models.calls_for(RoleTask.SELECT_PASSAGES)
    assert selections and isinstance(selections[0].payload, PassageSelectionInput)


def test_budget_exhausted_during_research_exits_error():
    script = hawkish_script(fail={RoleTask.PLAN_QUERIES: BudgetExhausted("call cap")})
    record, world = run_episode(fed_file(), script)
    assert record.exit is Exit.ERROR and "budget" in record.exit_detail
    assert orders(world) == []


def test_a_critic_evidence_request_is_retrieved_and_routed_through_the_researcher():
    official = {MINUTES.doc_id, SPEECH.doc_id}

    def first_official_then_all(payload: PassageSelectionInput) -> list[str]:
        ids = [p.passage_id for p in payload.passages]
        if calls["n"] == 0:
            calls["n"] += 1
            return [i for i in ids if i.split("#")[0] in official]
        return ids

    calls = {"n": 0}
    script = hawkish_script(keep=first_official_then_all)
    hold = "The Committee holds the rate in December"
    script.scores[hold] = Spec(0.7, HAWKISH, evidence_request="officials federal funds rate steady")
    file = fed_file(documents=[MINUTES, SPEECH, NEWS, FUTURE_CANARY])
    record, world = run_episode(file, script)
    selections = world.models.calls_for(RoleTask.SELECT_PASSAGES)
    assert len(selections) == 2  # research, then the critic's follow-up
    assert any(pid.startswith(NEWS.doc_id) for pid in record.passage_ids)
    assert record.forecast is not None


def test_a_self_halt_condition_before_the_gate_halts_and_sends_nothing():
    world = make_world(fed_file(), hawkish_script())
    for i in range(10):  # two of the last ten orders did not fill: more than one in ten
        status = OrderStatus.UNFILLED if i < 2 else OrderStatus.FILLED
        world.store.save_order(
            OrderResult(
                order_id=f"old-{i}",
                client_order_id=f"old-{i}",
                episode_id="old",
                market_ticker=H0,
                side=Side.YES,
                requested_contracts=1,
                limit_price=Decimal("0.5"),
                status=status,
                placed_at=AS_OF - timedelta(days=2),
            )
        )
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    check_trace(record)
    assert record.exit is Exit.HALTED and "fill failures" in record.exit_detail
    assert world.store.get_halt().halted
    assert [o.order_id for o in orders(world)] == [f"old-{i}" for i in range(10)]


def test_too_little_explained_mass_exits_low_coverage():
    script = hawkish_script()
    for claim in script.scores:
        script.scores[claim] = Spec(0.2, HAWKISH)  # 0.4 of the outcome explained
    record, world = run_episode(fed_file(), script)
    assert record.exit is Exit.LOW_COVERAGE
    assert record.forecast is None and orders(world) == []


def test_a_rematch_over_a_shared_calendar_level_keeps_the_nodes_of_both_attempts():
    jan = fed_event(FED_JAN, close=FED_CLOSE + timedelta(days=50))
    dec = fed_event()
    file = fed_file(
        events=[dec, jan],
        order_books={**books_for(dec), **books_for(jan)},
        scheduled_events=[JOBS_DEC],  # before both closes: level 1 of both trees
    )
    # Asserting a resolution option at the jobs level fails the rules check.
    tree = {
        (JOBS_DEC.scheduled_event_id, None): [
            T("Strong jobs mean a hold", ("hold",)),
            T("Weak jobs mean a cut", ("cut_25",)),
        ]
    }
    record, world = run_episode(file, Script(matches=[FED, FED_JAN], tree=tree))
    assert record.exit is Exit.RULES_MISMATCH
    nodes = world.store.get_nodes(record.episode_id)
    assert len(nodes) == 4 and len({n.node_id for n in nodes}) == 4
    assert sorted(n.claim for n in nodes) == sorted(
        ["Strong jobs mean a hold", "Weak jobs mean a cut"] * 2
    )


def test_the_run_budget_stops_the_run_after_the_episode_is_recorded():
    file = fed_file()
    script = hawkish_script(fail={RoleTask.PLAN_QUERIES: RunBudgetExhausted("$25.00 spent")})
    world = make_world(file, script)
    with EpisodeController(world) as controller:
        with pytest.raises(RunBudgetExhausted):
            controller.run(file.trigger)
        thread = {"configurable": {"thread_id": episode_id_for(world.mode, file.trigger)}}
        assert controller.checkpointer.get_tuple(thread) is None
    [record] = world.store.list_episodes()
    check_trace(record)
    assert record.exit is Exit.ERROR and "run's dollar cap was reached" in record.exit_detail
    assert record.status is EpisodeStatus.DONE and orders(world) == []


class LiveExecutor:
    """The paper executor, reporting itself as a real (non-simulated) one, as the demo's is."""

    simulated = False

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


def real_orders_world(file, mode, policy=ReviewPolicy.PAUSE):
    world = make_world(file, hawkish_script(), policy=policy, mode=mode)
    return dataclasses.replace(world, executor=LiveExecutor(world.executor))


def test_demo_mode_runs_at_the_approval_stage_whatever_the_stored_stage():
    world = real_orders_world(fed_file(stage=Stage.BACKTEST), RunMode.DEMO)
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    assert record.gate is not None and record.gate.approved  # the stage check passed
    assert record.status is EpisodeStatus.PENDING_REVIEW
    assert world.store.get_stage().stage is Stage.BACKTEST  # the stored stage is unchanged


def test_real_orders_outside_demo_mode_need_the_stored_approval_stage():
    world = real_orders_world(fed_file(stage=Stage.BACKTEST), RunMode.SHADOW)
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    assert record.exit is Exit.GATE_REJECTED and "stage" in record.gate.reasons
    assert orders(world) == []


@pytest.mark.parametrize("stored", [Stage.BACKTEST, Stage.AUTONOMOUS])
def test_every_demo_order_waits_for_a_human_whatever_the_stored_stage(stored):
    file = autonomous_file(stage=stored)
    world = real_orders_world(file, RunMode.DEMO)
    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
    routing = record.routing
    assert routing is not None and routing.tier is RoutingTier.APPROVE and routing.would_be_auto
    assert record.status is EpisodeStatus.PENDING_REVIEW and orders(world) == []


def test_a_human_approved_fill_is_not_fill_tagged():
    controller, world, paused = paused_episode()
    with controller:
        record = controller.resume(paused.episode_id, approve(paused, world))
    assert record.exit is Exit.EXECUTED and not record.fill_tagged
