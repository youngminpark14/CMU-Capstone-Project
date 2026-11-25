"""Episode tallies and the pruning-audit aggregate on a handful of hand-built episodes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmfa.evaluation.summary import dollar_spread, episode_tallies, pruning_summary, spread
from pmfa.records.base import RunMode
from pmfa.records.decision import GateResult, Routing, RoutingTier
from pmfa.records.episode import EpisodeRecord, Exit, Usage
from pmfa.records.evidence import Trigger, TriggerKind

from .factories import T0

D = Decimal


def episode(
    n: int,
    exit: Exit | None,
    *,
    calls: int,
    cost: str,
    seconds: float,
    gate: GateResult | None = None,
    routing: Routing | None = None,
) -> EpisodeRecord:
    return EpisodeRecord(
        episode_id=f"ep-{n}",
        mode=RunMode.BACKTEST,
        trigger=Trigger(trigger_id=f"tr-{n}", kind=TriggerKind.SCHEDULE, at=T0, event_ticker="EV"),
        as_of=T0,
        event_ticker="EV",
        exit=exit,
        gate=gate,
        routing=routing,
        usage=Usage(calls=calls, cost_usd=D(cost), seconds=seconds),
        started_at=T0,
    )


EPISODES = [
    episode(1, Exit.NO_EDGE, calls=4, cost="0.10", seconds=2.0),
    episode(
        2,
        Exit.GATE_REJECTED,
        calls=10,
        cost="0.50",
        seconds=8.0,
        gate=GateResult(approved=False, reasons=["per_event_cap", "liquidity_floor"]),
    ),
    episode(
        3,
        Exit.EXECUTED,
        calls=12,
        cost="0.60",
        seconds=5.0,
        gate=GateResult(approved=True),
        routing=Routing(tier=RoutingTier.APPROVE, would_be_auto=True),
    ),
    episode(
        4,
        Exit.GATE_REJECTED,
        calls=6,
        cost="0.20",
        seconds=3.0,
        gate=GateResult(approved=False, reasons=["per_event_cap"]),
    ),
    episode(
        5,
        None,
        calls=0,
        cost="0",
        seconds=0.0,
        routing=Routing(tier=RoutingTier.AUTO),
    ),
]


def test_exits_by_reason_most_common_first():
    tallies = episode_tallies(EPISODES)
    assert tallies.n_episodes == 5
    assert list(tallies.exits.items()) == [
        ("gate_rejected", 2),
        ("executed", 1),
        ("no_edge", 1),
        ("unfinished", 1),
    ]


def test_gate_rejections_by_reason():
    tallies = episode_tallies(EPISODES)
    # Two rejected proposals failing three checks between them; the approval is not counted.
    assert tallies.gate_rejections == {"per_event_cap": 2, "liquidity_floor": 1}
    assert tallies.n_gate_rejected == 2


def test_routing_tiers():
    tallies = episode_tallies(EPISODES)
    assert tallies.routing_tiers == {"approve": 1, "auto": 1}
    assert tallies.n_would_be_auto == 1


def test_cost_calls_and_latency_per_episode():
    tallies = episode_tallies(EPISODES)
    # Cost: 0.10 + 0.50 + 0.60 + 0.20 + 0 = 1.40, mean 0.28; sorted 0, 0.10, 0.20, 0.50, 0.60.
    assert (tallies.cost_usd.mean, tallies.cost_usd.median, tallies.cost_usd.max) == (
        D("0.28"),
        D("0.20"),
        D("0.60"),
    )
    # Calls: 4 + 10 + 12 + 6 + 0 = 32, mean 6.4; sorted 0, 4, 6, 10, 12.
    assert (tallies.calls.mean, tallies.calls.median, tallies.calls.max) == (
        pytest.approx(6.4),
        6.0,
        12.0,
    )
    # Seconds: 2 + 8 + 5 + 3 + 0 = 18, mean 3.6; sorted 0, 2, 3, 5, 8.
    assert tallies.latency_seconds.mean == pytest.approx(3.6)
    assert (tallies.latency_seconds.median, tallies.latency_seconds.max) == (3.0, 8.0)


def test_spreads_of_even_and_empty_lists():
    # Median of 1, 2, 3, 4 is (2 + 3) / 2.
    assert dollar_spread([D("4"), D("1"), D("3"), D("2")]).median == D("2.5")
    assert spread([4.0, 1.0, 3.0, 2.0]).median == 2.5
    assert spread([]).mean is None and dollar_spread([]).max is None
    empty = episode_tallies([])
    assert empty.n_episodes == 0 and empty.exits == {} and empty.calls.n == 0


def test_pruning_summary_mean_and_rate():
    # Mass (0 + 0.3 + 0.6 + 0.1) / 4 = 0.25; frozen most likely in 1 of 4 episodes.
    audit = pruning_summary([(0.0, False), (0.3, True), (0.6, False), (0.1, False)])
    assert audit.n_episodes == 4
    assert audit.mean_frozen_posterior_mass == pytest.approx(0.25)
    assert audit.frozen_most_likely_rate == 0.25


def test_pruning_summary_of_nothing_and_of_bad_input():
    empty = pruning_summary([])
    assert empty.n_episodes == 0 and empty.mean_frozen_posterior_mass is None
    with pytest.raises(ValueError, match="outside"):
        pruning_summary([(1.2, False)])
