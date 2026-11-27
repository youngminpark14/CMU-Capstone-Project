"""Routing: each auto condition, the stage rule, and the stop conditions."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from pmfa.decision import route, size_event
from pmfa.records.base import Stage
from pmfa.records.decision import RoutingTier
from pmfa.records.episode import Calibration
from tests.decision.factories import CONFIG, inputs, risk, routing_context

# Confidence 0.04 sizes 7 contracts at 0.50: $3.50 plus a $0.13 fee, under the $10 risk unit.
SMALL = size_event(inputs(confidence=0.04), CONFIG).proposal
assert SMALL is not None and SMALL.cost < D("10")


def test_auto_when_every_condition_holds_at_the_autonomous_stage():
    routing = route(SMALL, routing_context())
    assert routing.tier is RoutingTier.AUTO
    assert routing.score == 0 and routing.reasons == [] and not routing.would_be_auto


@pytest.mark.parametrize("stage", [Stage.BACKTEST, Stage.SHADOW, Stage.APPROVAL])
def test_would_be_auto_goes_to_review_below_the_autonomous_stage(stage):
    routing = route(SMALL, routing_context(stage=stage))
    assert routing.tier is RoutingTier.APPROVE
    assert routing.would_be_auto and routing.score == 0


CONDITIONS = [
    ("cost_under_risk_unit", SMALL.model_copy(update={"cost": D("10")}), {}),
    ("liquidity_floor", SMALL, {"liquidity_dollars": D("499")}),
    (
        "category_calibrated",
        SMALL,
        {"calibration": Calibration(category="c", n_resolved=29, calibration_error=0.01)},
    ),
    (
        "category_calibrated",
        SMALL,
        {"calibration": Calibration(category="c", n_resolved=100, calibration_error=0.10)},
    ),
    (
        "category_calibrated",
        SMALL,
        {"calibration": Calibration(category="c", n_resolved=100, calibration_error=None)},
    ),
    ("protected_not_driving", SMALL.model_copy(update={"protected_drove_size": True}), {}),
    ("price_stable", SMALL, {"price_move": D("-0.04")}),
]


@pytest.mark.parametrize(
    ("name", "proposal", "overrides"), CONDITIONS, ids=[c[0] for c in CONDITIONS]
)
def test_each_failed_auto_condition_sends_the_proposal_to_review(name, proposal, overrides):
    routing = route(proposal, routing_context(**overrides))
    assert routing.tier is RoutingTier.APPROVE
    assert routing.score == 1 and routing.reasons == [name]
    assert not routing.would_be_auto


def test_a_three_cent_move_is_still_stable():
    assert route(SMALL, routing_context(price_move=D("0.03"))).tier is RoutingTier.AUTO


def test_the_score_counts_every_failed_condition():
    routing = route(
        SMALL.model_copy(update={"protected_drove_size": True}),
        routing_context(liquidity_dollars=D("0"), price_move=D("0.10")),
    )
    assert routing.score == 3
    assert routing.reasons == ["liquidity_floor", "protected_not_driving", "price_stable"]


@pytest.mark.parametrize(
    ("stop", "overrides"),
    [
        ("halt_flag", {"halted": True}),
        ("daily_loss_stop", {"risk": risk(loss_stop=True)}),
        ("drawdown_halt", {"risk": risk(drawdown_halt=True)}),
    ],
)
def test_any_stop_condition_halts(stop, overrides):
    routing = route(SMALL, routing_context(**overrides))
    assert routing.tier is RoutingTier.HALT
    assert routing.reasons[0] == stop
    assert not routing.would_be_auto
