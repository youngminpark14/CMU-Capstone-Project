"""The live pass asks again about every order whose outcome is unknown, before anything else acts."""

from __future__ import annotations

import pytest

from pmfa.episode import EpisodeController
from pmfa.live.loop import cursor_path, read_last_look, run_once, write_last_look
from pmfa.live.text import pass_lines
from pmfa.records.decision import ProposalStatus
from pmfa.records.episode import EpisodeStatus, Exit
from tests.episodes.support import AS_OF, doubtful

from .support import live_file, live_world, no_resolver


@pytest.fixture
def file(tmp_path):
    return live_file(tmp_path / "data")


def left_unknown(world, controller):
    """A first look, then the pass whose order answer is lost: its outcome is unknown."""
    run_once(world, controller, resolver=no_resolver)
    world.clock.set(AS_OF)
    summary = run_once(world, controller, resolver=no_resolver)
    [outcome] = summary.episodes
    assert outcome.status is EpisodeStatus.OUTCOME_UNKNOWN and outcome.exit is None
    assert summary.outcome_unknown == [outcome.proposal.proposal_id]
    assert summary.reconciled == []
    return summary, outcome


def test_a_pass_reports_an_unknown_outcome_and_the_next_pass_learns_it(file):
    world = doubtful(live_world(file), failures=1)
    with EpisodeController(world) as controller:
        unknown, outcome = left_unknown(world, controller)
        lines = "\n".join(pass_lines(unknown))
        assert "may stand on the exchange" in lines and "pmfa reconcile --mode shadow" in lines

        world.clock.advance(300)
        later = run_once(world, controller, resolver=no_resolver)
    pid = outcome.proposal.proposal_id
    [line] = later.reconciled
    assert pid in line and "executed" in line
    assert later.outcome_unknown == []
    record = world.store.get_episode(outcome.episode_id)
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    assert world.store.get_proposal(pid)[1] is ProposalStatus.EXECUTED
    assert len(world.executor.placed) == 1 and len(world.store.list_orders()) == 1


def test_the_pass_reconciles_before_its_triggers_run(file):
    """The same trigger fires again in the next pass: the reconcile step has already
    settled its episode, so the trigger's run finds it finished and sends nothing."""
    world = doubtful(live_world(file), failures=1)
    with EpisodeController(world) as controller:
        run_once(world, controller, resolver=no_resolver)
        before = read_last_look(cursor_path(world))
        world.clock.set(AS_OF)
        unknown = run_once(world, controller, resolver=no_resolver)
        [outcome] = unknown.episodes
        assert outcome.status is EpisodeStatus.OUTCOME_UNKNOWN
        write_last_look(cursor_path(world), before)  # the minutes are new again
        later = run_once(world, controller, resolver=no_resolver)
    [line] = later.reconciled
    assert outcome.proposal.proposal_id in line
    [rerun] = later.episodes
    assert rerun.episode_id == outcome.episode_id
    assert rerun.already_run and rerun.exit is Exit.EXECUTED
    assert len(world.executor.placed) == 1


def test_a_reconcile_that_fails_is_a_note_and_the_pass_goes_on(file):
    world = doubtful(live_world(file), failures=1)
    with EpisodeController(world) as controller:
        _, outcome = left_unknown(world, controller)
        pid = outcome.proposal.proposal_id
        world.executor.crash = True  # the next lookup dies with an unexpected error
        world.clock.advance(300)
        failed = run_once(world, controller, resolver=no_resolver)
        assert failed.reconciled == [] and failed.outcome_unknown == [pid]
        assert any(pid in n and "reconcile failed" in n for n in failed.notes)
        assert failed.equity is not None  # the rest of the pass ran

        world.clock.advance(300)
        later = run_once(world, controller, resolver=no_resolver)
    assert len(later.reconciled) == 1 and later.outcome_unknown == []
    assert len(world.executor.placed) == 1 and len(world.store.list_orders()) == 1


def test_an_episode_in_doubt_whose_thread_is_not_here_is_a_note_and_never_rerun(file):
    world = doubtful(live_world(file), failures=1)
    with EpisodeController(world) as controller:
        _, outcome = left_unknown(world, controller)
    world.clock.advance(300)
    with EpisodeController(world) as fresh:  # an empty in-memory checkpointer
        later = run_once(world, fresh, resolver=no_resolver)
    pid = outcome.proposal.proposal_id
    assert later.outcome_unknown == [pid]
    assert any(pid in n and "reconcile failed" in n for n in later.notes)
    assert len(world.executor.placed) == 1


def test_a_reconcile_still_without_an_answer_waits_for_the_next_pass(file):
    world = doubtful(live_world(file), failures=2)  # the send's own lookup, then one pass
    with EpisodeController(world) as controller:
        _, outcome = left_unknown(world, controller)
        world.clock.advance(300)
        later = run_once(world, controller, resolver=no_resolver)
        assert later.reconciled == []
        assert later.outcome_unknown == [outcome.proposal.proposal_id]
        world.clock.advance(300)
        last = run_once(world, controller, resolver=no_resolver)
    assert last.outcome_unknown == [] and len(last.reconciled) == 1
    assert len(world.executor.placed) == 1


def test_a_halted_pass_still_records_the_order_a_reconcile_finds(file):
    world = doubtful(live_world(file), failures=1)
    with EpisodeController(world) as controller:
        _, outcome = left_unknown(world, controller)
        world.store.set_halt("manual", AS_OF)
        world.clock.advance(300)
        later = run_once(world, controller, resolver=no_resolver)
    assert later.halted and len(later.reconciled) == 1
    record = world.store.get_episode(outcome.episode_id)
    assert record.exit is Exit.EXECUTED and len(world.store.list_orders()) == 1
