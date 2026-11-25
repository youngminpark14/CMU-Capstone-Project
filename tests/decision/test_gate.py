"""The gate: each check failing alone, and the gate never changing an order."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from pmfa.config import Config, FollowConfig
from pmfa.decision import GateContext, gate, size_event
from pmfa.records.base import Stage
from pmfa.records.decision import GateResult, Proposal
from pmfa.records.market import PriceRange, Side
from tests.decision.factories import (
    CONFIG,
    CUT,
    HOLD,
    NOW,
    PROPERTY,
    book,
    gate_context,
    inputs,
    market,
    position,
    risk,
    secondary,
    series,
)

SIZED = inputs()
PROPOSAL = size_event(SIZED, CONFIG).proposal
assert PROPOSAL is not None  # 25 YES at 0.50, cost $12.94, bound 0.66


def _context(**overrides: object) -> GateContext:
    return gate_context(SIZED, **overrides)


def test_the_baseline_passes_every_check():
    result = gate(PROPOSAL, _context())
    assert result.approved and result.reasons == []
    assert {c.name for c in result.checks} == {
        "context_matches",
        "halt",
        "stage",
        "blocklist",
        "followed_series",
        "origin_rule",
        "liquidity_floor",
        "positive_size",
        "contracts_per_order",
        "dollars_per_market",
        "dollars_per_event",
        "daily_loss_stop",
        "drawdown_halt",
        "limit_order",
        "immediate_or_cancel",
        "valid_tick",
        "edge_bound",
        "min_edge",
        "not_expired",
    }


def _follow_without_fed() -> Config:
    follow = FollowConfig(
        series=[
            s.model_copy(update={"enabled": s.ticker != "KXFEDDECISION" and s.enabled})
            for s in CONFIG.follow.series
        ]
    )
    return Config(follow=follow)


ALONE: list[tuple[str, Proposal, GateContext]] = [
    ("context_matches", PROPOSAL, _context(market=market(CUT))),
    ("halt", PROPOSAL, _context(halted=True)),
    ("stage", PROPOSAL, _context(simulated=False, stage=Stage.SHADOW)),
    ("blocklist", PROPOSAL, _context(config=Config(follow=FollowConfig(blocklist=[HOLD])))),
    (
        "blocklist",
        PROPOSAL,
        _context(config=Config(follow=FollowConfig(blocklist=["KXFEDDECISION"]))),
    ),
    ("followed_series", PROPOSAL, _context(config=_follow_without_fed())),
    ("origin_rule", PROPOSAL, _context(origins=[secondary(1)])),
    ("liquidity_floor", PROPOSAL, _context(liquidity_dollars=D("499.99"))),
    ("positive_size", PROPOSAL.model_copy(update={"contracts": 0}), _context()),
    ("contracts_per_order", PROPOSAL.model_copy(update={"contracts": 26}), _context()),
    # 80 YES held at 0.50 is $40; adding $12.94 passes the $50 market cap.
    ("dollars_per_market", PROPOSAL, _context(positions=[position(contracts=80)])),
    # $95 elsewhere in the event plus $12.94 passes the $100 event cap.
    ("dollars_per_event", PROPOSAL, _context(positions=[position(ticker=CUT, contracts=190)])),
    ("daily_loss_stop", PROPOSAL, _context(risk=risk(loss_stop=True))),
    ("daily_loss_stop", PROPOSAL, _context(risk=risk(day_pnl="-30"))),
    ("drawdown_halt", PROPOSAL, _context(risk=risk(drawdown_halt=True))),
    ("drawdown_halt", PROPOSAL, _context(risk=risk(drawdown="0.10"))),
    ("limit_order", PROPOSAL.model_copy(update={"order_type": "market"}), _context()),
    (
        "immediate_or_cancel",
        PROPOSAL.model_copy(update={"time_in_force": "good_till_canceled"}),
        _context(),
    ),
    (
        "valid_tick",
        PROPOSAL,
        _context(
            market=market(price_ranges=[PriceRange(start="0.01", end="0.99", step=D("0.02"))])
        ),
    ),
    ("not_expired", PROPOSAL, _context(now=PROPOSAL.expires_at)),
]


@pytest.mark.parametrize(("name", "proposal", "context"), ALONE, ids=[a[0] for a in ALONE])
def test_each_check_fails_alone(name, proposal, context):
    result = gate(proposal, context)
    assert not result.approved
    assert result.reasons == [name]
    failed = next(c for c in result.checks if c.name == name)
    assert failed.detail


def test_min_edge_fails_alone_when_fee_rounding_breaks_the_edge():
    # One contract at 0.50 with p = 0.5385: 0.021 of edge before rounding, 0.0185 after.
    # The book is YES 0.49 / 0.50 (mid 0.495); shrinking 0.582 halfway onto it gives 0.5385.
    tight_book = book(yes_bids=[("0.49", "100")], no_bids=[("0.50", "100")])
    tight = inputs(forecast={HOLD: 0.582}, books={HOLD: tight_book}, shrinkage=0.5)
    one = PROPOSAL.model_copy(update={"contracts": 1})
    result = gate(one, gate_context(tight))
    assert result.reasons == ["min_edge"]


def test_a_limit_above_the_edge_bound_fails_the_bound_and_the_edge():
    # The bound is 0.66 for p = 0.70; a limit of 0.67 is a valid tick past it.
    above = PROPOSAL.model_copy(update={"limit_price": D("0.67")})
    assert gate(above, _context()).reasons == ["edge_bound", "min_edge"]


def test_the_gate_recomputes_the_bound_from_the_current_book():
    # The market fell to 0.44/0.46: shrunk onto the new mid, a 0.50 YES limit has no edge left.
    moved = SIZED.books[HOLD].model_copy(
        update={
            "yes_bids": [SIZED.books[HOLD].yes_bids[0].model_copy(update={"price": D("0.44")})],
            "no_bids": [SIZED.books[HOLD].no_bids[0].model_copy(update={"price": D("0.54")})],
        }
    )
    sc = SIZED.series_config.model_copy(update={"shrinkage": 1.0})
    result = gate(PROPOSAL, _context(book=moved, series_config=sc))
    assert "edge_bound" in result.reasons and "min_edge" in result.reasons


def test_real_orders_are_allowed_at_the_approval_stage():
    assert gate(PROPOSAL, _context(simulated=False, stage=Stage.APPROVAL)).approved
    assert gate(PROPOSAL, _context(simulated=True, stage=Stage.BACKTEST)).approved


def test_selling_a_holding_nets_it_down_within_the_caps():
    # Holding 120 NO at $0.40 ($48) and buying 25 YES leaves 95 NO ($38) plus the fee.
    held = position(side=Side.NO, contracts=120, avg="0.40")
    assert gate(PROPOSAL, _context(positions=[held])).approved


def test_every_failed_check_is_listed():
    result = gate(
        PROPOSAL.model_copy(update={"contracts": 26}),
        _context(halted=True, origins=[], liquidity_dollars=D("0")),
    )
    assert result.reasons == ["halt", "origin_rule", "liquidity_floor", "contracts_per_order"]


def test_an_unknown_fee_type_fails_the_fee_checks_instead_of_raising():
    result = gate(PROPOSAL, _context(series=series(fee_type="flat")))
    assert not result.approved
    assert {"dollars_per_market", "edge_bound", "min_edge"} <= set(result.reasons)


def test_the_result_carries_no_order_and_the_proposal_is_frozen():
    assert set(GateResult.model_fields) == {"approved", "reasons", "checks"}
    with pytest.raises(ValidationError):
        PROPOSAL.contracts = 1  # type: ignore[misc]


@PROPERTY
@given(
    contracts=st.integers(min_value=-5, max_value=60),
    limit=st.integers(min_value=1, max_value=99).map(lambda n: D(n) / 100),
    side=st.sampled_from(Side),
    halted=st.booleans(),
    stage=st.sampled_from(Stage),
    simulated=st.booleans(),
    minutes=st.integers(min_value=-20, max_value=20),
    held=st.integers(min_value=0, max_value=200),
)
def test_the_gate_never_returns_a_modified_order(
    contracts, limit, side, halted, stage, simulated, minutes, held
):
    proposal = PROPOSAL.model_copy(
        update={"contracts": contracts, "limit_price": limit, "side": side}
    )
    before = proposal.model_dump()
    positions = [position(contracts=held)] if held else []
    context = _context(
        halted=halted,
        stage=stage,
        simulated=simulated,
        now=NOW + timedelta(minutes=minutes),
        positions=positions,
    )
    context_before = context.model_dump()
    result = gate(proposal, context)
    assert proposal.model_dump() == before
    assert context.model_dump() == context_before
    assert isinstance(result, GateResult)
    assert result.approved == (result.reasons == [])
    assert result.reasons == [c.name for c in result.checks if not c.passed]
    assert not any(isinstance(v, Proposal) for v in result.__dict__.values())
