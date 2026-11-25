"""``run_once`` end to end on a small recorded world, and ``run_loop`` with a fake sleep."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pmfa.episode import EpisodeController
from pmfa.errors import RunBudgetExhausted
from pmfa.live.loop import cursor_path, read_last_look, run_loop, run_once
from pmfa.records.decision import ProposalStatus, ReviewPolicy
from pmfa.records.episode import EpisodeStatus, Exit
from pmfa.records.evidence import TriggerKind
from pmfa.records.roles import RoleTask
from tests.episodes.support import AS_OF, FED, hawkish_script

from .support import MINUTES, live_file, live_world, mid_candles, no_resolver


@pytest.fixture
def file(tmp_path):
    return live_file(tmp_path / "data")


def test_a_first_look_indexes_and_a_second_runs_one_episode_for_the_new_minutes(file):
    world = live_world(file)
    with EpisodeController(world) as controller:
        first = run_once(world, controller, resolver=no_resolver)
        assert first.first_look and first.triggers == [] and first.episodes == []
        assert first.open_events == [FED] and first.events_indexed == 1
        assert read_last_look(cursor_path(world)) == first.started_at

        world.clock.set(AS_OF)  # the minutes are out now
        second = run_once(world, controller, resolver=no_resolver)

    assert not second.first_look and second.documents_new == 1
    [trigger] = second.triggers
    assert trigger.kind is TriggerKind.NEW_DOCUMENT and trigger.doc_id == MINUTES.doc_id
    [outcome] = second.episodes
    assert outcome.exit is Exit.EXECUTED and outcome.event_ticker == FED
    assert outcome.proposal is not None and outcome.proposal.side.value == "no"
    [order] = world.store.list_orders()
    assert order.filled_contracts == outcome.proposal.contracts
    assert second.equity is not None
    assert world.store.equity_series()[-1][1] == second.equity


def test_the_same_pass_run_twice_starts_no_second_episode(file):
    world = live_world(file)
    with EpisodeController(world) as controller:
        run_once(world, controller, resolver=no_resolver)
        world.clock.set(AS_OF)
        before = read_last_look(cursor_path(world))
        once = run_once(world, controller, resolver=no_resolver)
        # Rewind the last look, as if the pass had crashed before saving it, and run again.
        from pmfa.live.loop import write_last_look

        write_last_look(cursor_path(world), before)
        twice = run_once(world, controller, resolver=no_resolver)

    assert once.triggers == twice.triggers
    assert [o.episode_id for o in once.episodes] == [o.episode_id for o in twice.episodes]
    assert twice.episodes[0].already_run and not once.episodes[0].already_run
    assert len(world.store.list_episodes()) == 1
    assert len(world.store.list_orders()) == 1


def test_a_later_pass_does_not_retrigger_the_same_document(file):
    world = live_world(file)
    with EpisodeController(world) as controller:
        run_once(world, controller, resolver=no_resolver)
        world.clock.set(AS_OF)
        run_once(world, controller, resolver=no_resolver)
        world.clock.advance(60)
        third = run_once(world, controller, resolver=no_resolver)
    assert third.triggers == [] and third.documents_new == 0


def test_a_price_move_starts_an_episode_that_never_trades_without_new_evidence(tmp_path):
    hold = f"{FED}-H0"
    candles = {
        hold: mid_candles(
            hold, [(AS_OF - timedelta(minutes=50), "0.62"), (AS_OF - timedelta(minutes=5), "0.66")]
        )
    }
    file = live_file(tmp_path / "data", candles=candles, documents=[])
    world = live_world(file, start=AS_OF)
    with EpisodeController(world) as controller:
        summary = run_once(world, controller, resolver=no_resolver)
    [trigger] = summary.triggers
    assert trigger.kind is TriggerKind.PRICE_MOVE and trigger.event_ticker == FED
    [outcome] = summary.episodes
    assert outcome.exit is not Exit.EXECUTED and world.store.list_orders() == []


def test_a_paused_proposal_waits_and_then_expires_in_a_later_pass(file):
    world = live_world(file, policy=ReviewPolicy.PAUSE)
    with EpisodeController(world) as controller:
        run_once(world, controller, resolver=no_resolver)
        world.clock.set(AS_OF)
        paused = run_once(world, controller, resolver=no_resolver)
        [outcome] = paused.episodes
        assert outcome.status is EpisodeStatus.PENDING_REVIEW
        assert paused.pending_review == [outcome.proposal.proposal_id]

        world.clock.advance(timedelta(minutes=11))
        later = run_once(world, controller, resolver=no_resolver)
    assert later.expired == [outcome.proposal.proposal_id] and later.pending_review == []
    record = world.store.get_episode(outcome.episode_id)
    assert record.exit is Exit.EXPIRED
    assert world.store.get_proposal(outcome.proposal.proposal_id)[1] is ProposalStatus.EXPIRED
    assert world.store.list_orders() == []


def test_when_halted_the_episode_runs_but_no_order_is_placed(file):
    world = live_world(file)
    world.store.set_halt("manual", AS_OF)
    with EpisodeController(world) as controller:
        run_once(world, controller, resolver=no_resolver)
        world.clock.set(AS_OF)
        summary = run_once(world, controller, resolver=no_resolver)
    assert summary.halted and summary.halt_reason == "manual"
    [outcome] = summary.episodes
    assert outcome.exit is Exit.HALTED
    assert world.store.list_orders() == []


def test_the_resolver_is_called_with_the_clock_time_and_its_answer_reported(file):
    seen = []

    def resolver(world, *, now=None):  # type: ignore[no-untyped-def]
        seen.append(now)
        return ["KXFEDDECISION-26OCT-H0 resolved yes"]

    world = live_world(file)
    with EpisodeController(world) as controller:
        summary = run_once(world, controller, resolver=resolver)
    assert seen == [world.clock.now()]
    assert summary.resolved == ["KXFEDDECISION-26OCT-H0 resolved yes"]


def test_the_loop_waits_on_the_clock_between_passes(file):
    world = live_world(file)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        world.clock.advance(seconds)

    seen = []
    with EpisodeController(world) as controller:
        passes = run_loop(
            world,
            controller,
            interval_seconds=1800,
            max_passes=3,
            sleep=sleep,
            on_pass=seen.append,
            resolver=no_resolver,
        )
    assert passes == 3 and sleeps == [1800.0, 1800.0]
    assert [s.started_at for s in seen] == [
        seen[0].started_at + timedelta(seconds=1800 * i) for i in range(3)
    ]
    # The minutes (published 30 minutes into the first wait) start exactly one episode.
    assert [len(s.episodes) for s in seen] == [0, 1, 0]


def test_the_loop_stops_when_the_runs_dollar_cap_is_reached(file):
    script = hawkish_script(fail={RoleTask.MATCH: RunBudgetExhausted("run cap $25 reached")})
    world = live_world(file, script=script)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        world.clock.advance(seconds)

    seen = []
    with EpisodeController(world) as controller:
        passes = run_loop(
            world,
            controller,
            interval_seconds=1800,
            sleep=sleep,
            on_pass=seen.append,
            resolver=no_resolver,
        )
    assert passes == 2
    assert "dollar cap" in seen[-1].stopped
    assert seen[-1].episodes[0].exit is Exit.ERROR


def test_a_pass_applies_the_stage_demotion_check(file, monkeypatch):
    from pmfa.execution import stages
    from pmfa.records.base import Stage
    from pmfa.records.episode import StageState

    calls = []

    def demote(self):
        calls.append(self.clock.now())
        return StageState(
            stage=Stage.BACKTEST, reason="two daily stops in a week", at=self.clock.now()
        )

    monkeypatch.setattr(stages.StageManager, "check_demotion", demote)
    world = live_world(file)
    with EpisodeController(world) as controller:
        summary = run_once(world, controller, resolver=no_resolver)
    assert len(calls) == 1
    assert any("stage demoted to backtest" in note for note in summary.notes)
