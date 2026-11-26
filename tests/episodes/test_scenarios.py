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
from pmfa.errors import AsOfViolation, BudgetExhausted, ModelOutputError, RunBudgetExhausted
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
from pmfa.records.evidence import ScheduledEvent, Trigger, TriggerKind
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
    book,
    books_for,
    calibrated_ledger,
    check_trace,
    cpi_file,
    doc_trigger,
    fed_event,
    fed_file,
    fed_series,
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


HOLD_DIST = {"hold": 0.80, "cut_25": 0.05, "hike_25": 0.10, "cut_gt25": 0.03, "hike_gt25": 0.02}
HIKE_DIST = {"hold": 0.05, "cut_25": 0.02, "hike_25": 0.90, "cut_gt25": 0.01, "hike_gt25": 0.02}


def jobs_level_episode(scores: dict[str, tuple[float, dict[str, float]]], prices, kelly="0.25"):
    """A jobs level of first-level scenarios (beam width 1), each with one resolution child
    carrying the same distribution. ``scores`` maps each claim to (probability, distribution)."""
    jobs = JOBS_DEC.scheduled_event_id
    tree: dict = {(jobs, None): [T(claim) for claim in scores]}
    specs: dict[str, Spec] = {}
    for claim, (probability, dist) in scores.items():
        leaf = f"Resolve after: {claim}"
        tree[(RESOLUTION, claim)] = [T(leaf, (max(dist, key=dist.get),))]
        specs[claim] = Spec(probability, dist)
        specs[leaf] = Spec(1.0, dist)
    event = fed_event(prices={**DEFAULT_PRICES, **prices})
    file = fed_file(
        event=event,
        order_books=books_for(event, depth="5000"),
        scheduled_events=[JOBS_DEC],
        config_overrides={"forecast": {"beam_width": 1}, "sizing": {"kelly_fraction": kelly}},
    )
    record, world = run_episode(file, Script(tree=tree, scores=specs))
    nodes = {n.claim: n for n in world.store.get_nodes(record.episode_id) if n.depth == 1}
    return record, nodes


def test_06_a_tail_scenario_whose_removal_flips_the_side_takes_the_protected_slot():
    """Without the cut scenario sizing buys C25 NO instead of C25 YES. At this Kelly fraction
    the cost move stays under one risk unit, so only the flip rule protects it."""
    prices = {"H0": ("0.65", "0.67"), "C25": ("0.19", "0.21"), "H25": ("0.09", "0.11")}
    record, nodes = jobs_level_episode(
        {
            "Payrolls come in strong": (0.58, HOLD_DIST),
            "Payrolls collapse": (0.27, B_DIST),
            "Payrolls are mixed": (0.15, HOLD_DIST),
        },
        prices,
        kelly="0.1",
    )
    collapse = nodes["Payrolls collapse"]
    assert (collapse.status, collapse.reason) == (NodeStatus.EXPANDED, "protected")
    assert nodes["Payrolls are mixed"].status is NodeStatus.FROZEN
    assert record.forecast is not None and record.forecast.protected_node_id == collapse.node_id
    assert not record.forecast.protected_drove_size  # the move is within one risk unit


def test_06_of_two_trade_changing_tail_scenarios_the_one_that_moves_the_size_most_is_protected():
    """Removing the cut scenario moves the trade by about $22 (H0 NO to C25 NO); removing the
    hike scenario flips it to C25 YES, a move of about $6. Both qualify; the larger wins."""
    prices = {"H0": ("0.63", "0.65"), "C25": ("0.19", "0.21"), "H25": ("0.09", "0.11")}
    _record, nodes = jobs_level_episode(
        {
            "Payrolls come in strong": (0.60, A_DIST),
            "Payrolls collapse": (0.24, B_DIST),
            "Payrolls surge": (0.16, HIKE_DIST),
        },
        prices,
    )
    collapse, surge = nodes["Payrolls collapse"], nodes["Payrolls surge"]
    assert (collapse.status, collapse.reason) == (NodeStatus.EXPANDED, "protected")
    assert surge.status is NodeStatus.FROZEN


def test_06_a_tail_scenario_that_changes_nothing_is_frozen_and_nothing_is_protected():
    prices = {"H0": ("0.63", "0.65"), "C25": ("0.19", "0.21")}
    record, nodes = jobs_level_episode(
        {"Payrolls come in strong": (0.85, A_DIST), "Payrolls are mixed": (0.15, A_DIST)}, prices
    )
    assert nodes["Payrolls are mixed"].status is NodeStatus.FROZEN
    assert all(n.reason != "protected" for n in nodes.values())
    assert record.forecast is not None and record.forecast.protected_node_id is None


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
    # The first proposal (NO at the 0.68 limit) was cancelled when nothing was offered at
    # its limit, and the recompute proposed nothing in its place.
    [(first, status)] = world.store.list_proposals()
    assert (first.market_ticker, first.side, first.limit_price) == (C25, Side.NO, Decimal("0.68"))
    assert status is ProposalStatus.CANCELLED
    # What the recompute decided: on the fresh book its best candidate was the same side at
    # the new 0.73 ask, and its edge after fees was below the minimum, so it proposed nothing.
    sized = [
        s.data["best"] for s in record.trace if s.node == "size" and (s.data or {}).get("best")
    ]
    best = sized[-1]
    assert (best["market_ticker"], Side(best["side"])) == (C25, Side.NO)
    assert Decimal(str(best["ask"])) == Decimal("0.73")
    assert Decimal(str(best["edge"])) < world.config.sizing.min_edge


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


class LeakyKnowledge:
    """A knowledge port whose evidence search trips an as-of check, as the backtest's
    canary guard does when a planted passage dated after the window comes back."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def search_evidence(self, queries, **kwargs):
        raise AsOfViolation("a passage published after the as-of time was returned")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_13_an_as_of_violation_from_a_port_aborts_the_run_instead_of_ending_the_episode():
    world = make_world(fed_file(), hawkish_script())
    world = dataclasses.replace(world, knowledge=LeakyKnowledge(world.knowledge))
    with EpisodeController(world) as controller, pytest.raises(AsOfViolation, match="as-of"):
        controller.run(world.file.trigger)
    assert all(e.status is not EpisodeStatus.DONE for e in world.store.list_episodes())
    assert orders(world) == []


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


# --------------------------------------------------------------------------- a held event


def recheck(world, minutes: int) -> Trigger:
    """A scheduled re-check of the Fed event ``minutes`` after the first episode."""
    at = AS_OF + timedelta(minutes=minutes)
    world.clock.set(at)  # type: ignore[attr-defined]
    return Trigger(
        trigger_id=f"recheck-{minutes}", kind=TriggerKind.SCHEDULE, at=at, event_ticker=FED
    )


def test_a_re_forecast_of_a_held_event_with_the_same_forecast_sends_no_second_order():
    """The first episode buys 19 NO of the cut. The re-check reaches the same forecast, so
    its target is the holding it already has, and there is no difference to trade.

    No proposal is made here, so the holding sizing read is shown by the test below, whose
    proposal carries it as ``position_before``."""
    world = make_world(fed_file(), hawkish_script())
    with EpisodeController(world) as controller:
        first = controller.run(world.file.trigger)
        assert first.exit is Exit.EXECUTED and first.proposal is not None
        bought = first.proposal.contracts
        second = controller.run(recheck(world, 5))
    check_trace(second)
    assert second.exit is Exit.NO_EDGE
    assert "holding is already at or above the target" in second.exit_detail
    assert second.proposal is None and len(orders(world)) == 1
    [position] = world.executor.positions(FED)
    assert (position.market_ticker, position.side, position.contracts) == (C25, Side.NO, bought)


def test_a_re_forecast_of_a_held_event_buys_only_the_difference_to_its_new_target():
    """A re-check that finds a cut less likely raises the NO target. It sizes against the
    holding the first episode bought and orders only the difference."""
    script = hawkish_script()
    world = make_world(fed_file(), script)
    with EpisodeController(world) as controller:
        first = controller.run(world.file.trigger)
        assert first.exit is Exit.EXECUTED and first.proposal is not None
        bought = first.proposal.contracts
        # The scripted critic reads its scores when called, so the re-check gets a new forecast.
        less_cut = {
            "hike_gt25": 0.01,
            "hike_25": 0.04,
            "hold": 0.80,
            "cut_25": 0.08,
            "cut_gt25": 0.07,
        }
        for claim, spec in script.scores.items():
            script.scores[claim] = dataclasses.replace(spec, options=less_cut)
        second = controller.run(recheck(world, 10))
    check_trace(second)
    assert second.exit is Exit.EXECUTED
    p = second.proposal
    assert p is not None and (p.market_ticker, p.side) == (C25, Side.NO)
    assert p.position_before == -bought  # a NO holding counts as negative
    assert p.position_target < p.position_before
    assert p.contracts == p.position_before - p.position_target
    assert [o.filled_contracts for o in orders(world)] == [bought, p.contracts]
    [position] = world.executor.positions(FED)
    assert (position.side, position.contracts) == (Side.NO, bought + p.contracts)


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

    def find_order(self, intent):
        return self._inner.find_order(intent)

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


# --------------------------------------------------------------------------- prices and recompute


def test_08_after_a_recompute_the_ledger_keeps_the_forecast_time_price_and_no_tier():
    """The recompute re-reads the execution books, but the ledger's price is the one at
    forecast time, and the cancelled first proposal's routing tier does not stay on it."""
    file = fed_file(execution_books={C25: no_book(C25, [("0.27", "1000")])})
    record, world = run_episode(file, hawkish_script())
    assert record.exit is Exit.NO_EDGE
    assert record.routing is None and record.gate is None
    ledger = {e.market_ticker: e for e in world.store.ledger()}
    assert ledger[C25].market_price == pytest.approx(0.33)  # the as-of mid, not 0.305
    assert ledger[C25].tier == ""


def thin_hold_world(policy):
    """Only C25 qualifies (H0 is thin), at a small Kelly fraction, at the autonomous stage."""
    dist = {"hike_gt25": 0.01, "hike_25": 0.01, "hold": 0.90, "cut_25": 0.03, "cut_gt25": 0.05}
    books = books_for(fed_event())
    books[H0] = book(H0, [("0.61", "5")], [("0.37", "5")])
    file = autonomous_file(
        order_books=books, config_overrides={"sizing": {"kelly_fraction": "0.05"}}
    )
    return make_world(file, hawkish_script(dist), policy=policy), file


def move_after_first_read(world, moved):
    """C25's execution book is the as-of one on its first read (routing's), then ``moved``."""
    inner = world.executor.books
    reads = {"n": 0}

    def books(ticker):
        if ticker == C25:
            reads["n"] += 1
            if reads["n"] >= 2:
                return moved
        return inner(ticker)

    world.executor.books = books


def test_auto_routing_measures_the_price_move_from_the_forecast_time_price_after_a_recompute():
    # The market moves 7 cents (mid 0.33 -> 0.26) between routing's read and execute's.
    world, file = thin_hold_world(ReviewPolicy.PAUSE)
    move_after_first_read(world, no_book(C25, [("0.25", "1000")], yes_ask="0.27"))
    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
    check_trace(record)
    routes = [s for s in record.trace if s.node == "route" and s.kind == "action"]
    assert len(routes) == 2  # routed, recomputed, routed again
    assert record.routing is not None and record.routing.tier is RoutingTier.APPROVE
    assert "price_stable" in record.routing.reasons
    assert record.status is EpisodeStatus.PENDING_REVIEW and orders(world) == []


def test_after_a_recompute_that_trades_the_ledger_price_is_still_the_forecast_time_price():
    world, file = thin_hold_world(ReviewPolicy.FILL_TAGGED)
    move_after_first_read(world, no_book(C25, [("0.25", "1000")], yes_ask="0.27"))
    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
    assert record.exit is Exit.EXECUTED and record.fill_tagged
    ledger = {e.market_ticker: e for e in world.store.ledger()}
    assert ledger[C25].market_price == pytest.approx(0.33)
    assert ledger[C25].traded


def test_auto_needs_a_stable_price_during_the_episode():
    # NO is still offered at the limit, but the YES mid moved 0.33 -> 0.37: more than 3 cents.
    moved = no_book(C25, [("0.32", "1000")], yes_ask="0.42")
    file = autonomous_file(execution_books={C25: moved})
    record, world = run_episode(file, hawkish_script(), policy=ReviewPolicy.PAUSE)
    assert record.routing is not None and record.routing.tier is RoutingTier.APPROVE
    assert record.routing.reasons == ["price_stable"]
    assert record.status is EpisodeStatus.PENDING_REVIEW and orders(world) == []


# --------------------------------------------------------------------------- every exit is named


def test_a_named_event_whose_outcome_space_cannot_be_built_exits_no_match():
    event = fed_event()
    bad = [
        m.model_copy(update={"custom_strike": {"Hike": "0"}}) if m.ticker.endswith("-H26") else m
        for m in event.markets
    ]
    event = event.model_copy(update={"markets": bad})
    trigger = Trigger(trigger_id="sched-dup", kind=TriggerKind.SCHEDULE, at=AS_OF, event_ticker=FED)
    file = fed_file(event=event, order_books=books_for(event), trigger=trigger)
    world = make_world(file, hawkish_script())
    with EpisodeController(world) as controller:
        record = controller.run(trigger)
        again = controller.run(trigger)
    check_trace(record)
    assert record.exit is Exit.NO_MATCH and "mean hold" in record.exit_detail
    assert record.status is EpisodeStatus.DONE and record.finished_at is not None
    assert again == record and orders(world) == []


def test_an_unknown_fee_type_ends_the_episode_in_error_with_no_order():
    series = fed_series().model_copy(update={"fee_type": "some_new_fee_type"})
    record, world = run_episode(fed_file(series=[series]), hawkish_script())
    assert record.exit is Exit.ERROR and "UnknownFeeType" in record.exit_detail
    assert record.status is EpisodeStatus.DONE and orders(world) == []
    [stored] = world.store.list_episodes()
    assert stored == record


# --------------------------------------------------------------------------- orders and the exchange


class SpyExecutor:
    """The paper executor reporting itself as real (as the demo's is), recording every
    ``place`` call. ``on_book`` runs after each book read; ``on_place`` replaces placing."""

    simulated = False

    def __init__(self, inner, *, on_book=None, on_place=None) -> None:
        self._inner = inner
        self.placed: list = []
        self.on_book = on_book
        self.on_place = on_place

    def execution_book(self, ticker):
        found = self._inner.execution_book(ticker)
        if self.on_book is not None:
            self.on_book(ticker)
        return found

    def place(self, intent):
        self.placed.append(intent)
        if self.on_place is not None:
            return self.on_place(intent)
        return self._inner.place(intent)

    def find_order(self, intent):
        return self._inner.find_order(intent)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class FlakyExchange(SpyExecutor):
    """Accepts and fills the order on a separate ledger (the exchange's), then fails before
    answering, as a fills read that errors after the order POST would."""

    def __init__(self, inner, exchange) -> None:
        super().__init__(inner, on_place=self._accept_then_fail)
        self.exchange = exchange

    def _accept_then_fail(self, intent):
        from pmfa.errors import ExchangeError

        self.exchange.place(intent)
        raise ExchangeError("GET /portfolio/fills -> 503 service_unavailable")


class FlakyExchangeWithLookup(FlakyExchange):
    def find_order(self, intent):
        for o in self.exchange.store.list_orders():
            if o.client_order_id == intent.client_order_id:
                return o
        return None


def exchange_side(world):
    """A second paper account standing in for the exchange's own record of orders."""
    from pmfa.execution.paper import PaperExecutor
    from pmfa.state.store import MEMORY, SqliteStore
    from pmfa.world.recorded import RecordedBooks

    return PaperExecutor(
        SqliteStore(MEMORY), world.clock, world.config, world.exchange, RecordedBooks(world.file)
    )


def spied_world(file, wrap, policy=ReviewPolicy.FILL_TAGGED, mode=RunMode.SHADOW):
    world = make_world(file, hawkish_script(), policy=policy, mode=mode)
    return dataclasses.replace(world, executor=wrap(world))


def test_an_exchange_error_after_the_order_was_accepted_records_the_order_found_by_its_id():
    world = spied_world(fed_file(), lambda w: FlakyExchangeWithLookup(w.executor, exchange_side(w)))
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    check_trace(record)
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    [order] = orders(world)
    assert order.status is OrderStatus.FILLED and record.orders == [order]
    assert order.client_order_id == world.executor.placed[0].client_order_id
    assert len(world.executor.placed) == 1
    assert ledger_traded(world, C25)


def test_an_exchange_error_while_sending_with_no_order_found_ends_in_error_and_is_recorded():
    world = spied_world(fed_file(), lambda w: FlakyExchange(w.executor, exchange_side(w)))
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
        again = controller.run(world.file.trigger)
    check_trace(record)
    assert record.exit is Exit.ERROR and record.status is EpisodeStatus.DONE
    intent = world.executor.placed[0]
    assert "503" in record.exit_detail and intent.client_order_id in record.exit_detail
    assert again == record and len(world.executor.placed) == 1
    # The outcome is unknown, so nothing may say the order was refused, filled or never
    # placed: the proposal keeps its approval and no order is stored under the episode.
    [(_, status)] = world.store.list_proposals()
    assert status is ProposalStatus.APPROVED
    assert world.store.list_orders(episode_id=record.episode_id) == []


def ledger_traded(world, ticker) -> bool:
    return next(e for e in world.store.ledger() if e.market_ticker == ticker).traded


def test_an_order_the_exchange_rejected_ends_in_error_and_the_proposal_is_cancelled():
    def reject(intent):
        return OrderResult(
            order_id="",
            client_order_id=intent.client_order_id,
            episode_id=intent.episode_id,
            market_ticker=intent.market_ticker,
            side=intent.side,
            requested_contracts=intent.contracts,
            limit_price=intent.limit_price,
            status=OrderStatus.REJECTED,
            error="POST /portfolio/events/orders -> 400 invalid_order: price out of range",
            placed_at=AS_OF,
        )

    world = spied_world(fed_file(), lambda w: SpyExecutor(w.executor, on_place=reject))
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    check_trace(record)
    assert record.exit is Exit.ERROR and "invalid_order" in record.exit_detail
    [(_, status)] = world.store.list_proposals()
    assert status is ProposalStatus.CANCELLED
    [order] = orders(world)  # kept: a rejection counts toward the fill-failure halt
    assert order.status is OrderStatus.REJECTED and order.filled_contracts == 0


def test_an_unfilled_order_the_exchange_accepted_still_exits_executed():
    def unfilled(intent):
        return OrderResult(
            order_id="ord-unfilled",
            client_order_id=intent.client_order_id,
            episode_id=intent.episode_id,
            market_ticker=intent.market_ticker,
            side=intent.side,
            requested_contracts=intent.contracts,
            limit_price=intent.limit_price,
            status=OrderStatus.UNFILLED,
            placed_at=AS_OF,
        )

    file = fed_file()
    world = spied_world(file, lambda w: SpyExecutor(w.executor, on_place=unfilled))
    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
    assert record.exit is Exit.EXECUTED and "unfilled" in record.exit_detail


# --------------------------------------------------------------------------- 10, at execute


def test_10_b_a_halt_set_after_the_gate_approved_stops_the_order_before_it_is_sent(monkeypatch):
    """``pmfa halt`` from another process between the gate and execute: the execute node's
    own check is the last one before a real (non-simulated) executor."""
    import pmfa.episode.pipeline as pipeline

    real_route = pipeline.Pipeline.route

    def route_then_halt(self, state):
        update = real_route(self, state)
        self.world.store.set_halt("operator halt", self.world.clock.now())
        return update

    monkeypatch.setattr(pipeline.Pipeline, "route", route_then_halt)
    world = spied_world(fed_file(), lambda w: SpyExecutor(w.executor))
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    check_trace(record)
    assert record.gate is not None and record.gate.approved
    assert record.exit is Exit.HALTED and "operator halt" in record.exit_detail
    assert world.executor.placed == [] and orders(world) == []


def test_10_c_a_halt_set_while_execute_reads_the_book_stops_the_order(transport):
    world = spied_world(with_transport(fed_file(), transport), lambda w: SpyExecutor(w.executor))
    reads = {"n": 0}

    def halt_on_executes_read(ticker):
        reads["n"] += 1
        if reads["n"] == 2:  # routing reads the book first; execute's read is the second
            world.store.set_halt("operator halt", world.clock.now())

    world.executor.on_book = halt_on_executes_read
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    assert record.exit is Exit.HALTED
    assert world.executor.placed == [] and orders(world) == []


def test_10_d_a_halt_set_between_the_gate_and_execute_stops_before_the_book_is_read(
    monkeypatch,
):
    """Execute reads the halt flag before it reads the execution book, so a halted
    episode does not ask the exchange for a live book it will not trade on."""
    import pmfa.episode.pipeline as pipeline

    world = spied_world(fed_file(), lambda w: SpyExecutor(w.executor))
    reads = {"total": 0, "at_halt": None}

    def count_reads(ticker):
        reads["total"] += 1

    real_route = pipeline.Pipeline.route

    def route_then_halt(self, state):
        update = real_route(self, state)
        self.world.store.set_halt("operator halt", self.world.clock.now())
        reads["at_halt"] = reads["total"]
        return update

    world.executor.on_book = count_reads
    monkeypatch.setattr(pipeline.Pipeline, "route", route_then_halt)
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    check_trace(record)
    assert record.gate is not None and record.gate.approved
    assert record.exit is Exit.HALTED and "operator halt" in record.exit_detail
    assert world.executor.placed == [] and orders(world) == []
    [(_, status)] = world.store.list_proposals()
    assert status is ProposalStatus.CANCELLED
    assert reads["at_halt"] is not None
    assert reads["total"] == reads["at_halt"]  # no book was read once the flag was set


# --------------------------------------------------------------------------- 9, the gate re-run


def test_09_approval_re_runs_the_gate_at_the_current_price():
    """After the pause the YES mid rises to 0.40 while NO is still offered at the 0.68
    limit: the edge at the current price is gone, so the approved order is not sent."""
    controller, world, paused = paused_episode()
    with controller:
        world.file.execution_books[C25] = no_book(C25, [("0.32", "1000")], yes_ask="0.48")
        advance(world, 60)
        record = controller.resume(paused.episode_id, approve(paused, world))
    check_trace(record)
    assert record.exit is Exit.GATE_REJECTED
    assert record.gate is not None and {"edge_bound", "min_edge"} & set(record.gate.reasons)
    assert orders(world) == []


# --------------------------------------------------------------------------- 5, 12: the guards


def test_05_one_first_level_scenario_passing_the_rules_check_is_not_a_rules_mismatch():
    hold = "The Committee holds the rate in December"
    tree = {(RESOLUTION, None): [T(hold, ("hold",)), T("The Committee pauses", ("pause",))]}
    script = Script(matches=[FED], tree=tree, scores={hold: Spec(1.0, HAWKISH)})
    record, world = run_episode(fed_file(), script)
    assert record.exit is not Exit.RULES_MISMATCH
    assert record.forecast is not None
    assert len(world.models.calls_for(RoleTask.MATCH)) == 1  # no re-match


def test_12_the_fast_path_skips_a_calendar_level_inside_the_last_hour():
    at = FED_CLOSE - timedelta(minutes=30)
    inside = ScheduledEvent(
        scheduled_event_id="speech:2026-12-09",
        kind="speech",
        title="Remarks ten minutes before the close",
        scheduled_at=FED_CLOSE - timedelta(minutes=10),
    )
    file = fed_file(trigger=doc_trigger(MINUTES, at=at), scheduled_events=[inside])
    script = hawkish_script()
    script.tree[(inside.scheduled_event_id, None)] = [T("The remarks are hawkish")]
    script.scores["The remarks are hawkish"] = Spec(1.0, HAWKISH)
    record, world = run_episode(file, script)
    forecast = record.forecast
    assert forecast is not None and forecast.fast_path and forecast.depth_reached == 1
    generated = [c.payload.level for c in world.models.calls_for(RoleTask.GENERATE)]
    assert generated and all(level.is_resolution for level in generated)


# --------------------------------------------------------------------------- the loss stop


def test_the_daily_loss_stop_is_a_fraction_of_the_day_start_equity():
    """Day start 1,000 and equity 970.50 now: a 29.50 loss, under 3% of 1,000 (30), though
    over 3% of the current equity (29.115). The stage manager counts the same base."""
    from pmfa.execution.stages import StageManager

    world = make_world(fed_file(bankroll="970.50"), hawkish_script())
    world.store.record_equity(AS_OF - timedelta(hours=5), Decimal("1000"))
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    assert record.exit is Exit.EXECUTED
    check = next(c for c in record.gate.checks if c.name == "daily_loss_stop")
    assert check.passed
    assert StageManager(world.store, world.config, world.clock).stop_days() == []


def test_after_an_intraday_gain_a_proposal_sized_at_the_market_cap_passes_the_gate():
    """Equity 1,100 now, 1,000 at the day's start. Sizing caps the market at 1.2% of the
    current equity ($13.20); the gate's cap has the same base, so it does not reject a
    proposal the sizer put just under it (1.2% of the day start would be $12.00)."""
    file = fed_file(
        bankroll="1100",
        config_overrides={
            "sizing": {"kelly_fraction": "1"},
            "caps": {"per_market_fraction": "0.012"},
        },
    )
    world = make_world(file, hawkish_script())
    world.store.record_equity(AS_OF - timedelta(hours=5), Decimal("1000"))
    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
    assert record.proposal is not None and record.proposal.cost > Decimal("12")
    assert record.exit is Exit.EXECUTED, record.gate and record.gate.reasons
