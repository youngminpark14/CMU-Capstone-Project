"""The controller around the graph: pause and resume across objects, idempotency, the docs graph."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from pmfa.episode import EpisodeController, episode_id_for, mermaid, open_checkpointer
from pmfa.records.decision import ProposalStatus, Review, ReviewDecision, ReviewPolicy
from pmfa.records.episode import EpisodeStatus, Exit

from .support import (
    advance,
    check_trace,
    fed_file,
    file_world,
    hawkish_script,
    make_world,
    with_transport,
)


def test_a_pause_in_one_controller_resumes_in_a_second_built_over_the_same_files(
    tmp_path, transport
):
    file = with_transport(fed_file(), transport)
    checkpoints = tmp_path / "checkpoints.sqlite"

    first_world = file_world(file, tmp_path, hawkish_script(), seed=True)
    saver = open_checkpointer(checkpoints)
    with EpisodeController(first_world, checkpointer=saver) as first:
        paused = first.run(file.trigger)
    saver.conn.close()
    first_world.store.close()  # type: ignore[attr-defined]
    assert paused.status is EpisodeStatus.PENDING_REVIEW and paused.proposal is not None

    # A second, independent world and controller: new store connection, new checkpointer,
    # new models (no model is called after the pause), new clock.
    second_world = file_world(file, tmp_path, hawkish_script(), seed=False)
    advance(second_world, 90)
    saver = open_checkpointer(checkpoints)
    review = Review(
        proposal_id=paused.proposal.proposal_id,
        decision=ReviewDecision.APPROVE,
        decided_at=second_world.clock.now(),
    )
    with EpisodeController(second_world, checkpointer=saver) as second:
        record = second.resume(paused.episode_id, review)
        again = second.resume(paused.episode_id, review)  # a second resume does nothing
    saver.conn.close()

    check_trace(record)
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    assert again == record
    [order] = second_world.store.list_orders()
    assert order.requested_contracts == paused.proposal.contracts
    assert record.usage.calls == paused.usage.calls  # the meter of the pausing process is kept
    # The trace continues where the first process left it, numbered without gaps.
    assert record.trace[: len(paused.trace)] == paused.trace


def test_resuming_twice_does_not_place_a_second_order():
    file = fed_file()
    world = make_world(file, hawkish_script(), policy=ReviewPolicy.PAUSE)
    with EpisodeController(world) as controller:
        paused = controller.run(file.trigger)
        review = Review(
            proposal_id=paused.proposal.proposal_id,
            decision=ReviewDecision.APPROVE,
            decided_at=world.clock.now(),
        )
        first = controller.resume(paused.episode_id, review)
        second = controller.resume(paused.episode_id, review)
        rerun = controller.run(file.trigger)  # the same trigger: the same episode, not a new one
    assert first.exit is Exit.EXECUTED and second == first and rerun == first
    assert len(world.store.list_orders()) == 1
    assert world.store.get_proposal(paused.proposal.proposal_id)[1] is ProposalStatus.EXECUTED


def test_a_finished_episode_thread_is_dropped_from_the_checkpointer():
    file = fed_file()
    world = make_world(file, hawkish_script())
    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
        config = {"configurable": {"thread_id": record.episode_id}}
        assert controller.checkpointer.get_tuple(config) is None
    assert record.episode_id == episode_id_for(world.mode, file.trigger)


def test_a_paused_episode_keeps_its_thread_until_resumed():
    file = fed_file()
    world = make_world(file, hawkish_script(), policy=ReviewPolicy.PAUSE)
    with EpisodeController(world) as controller:
        paused = controller.run(file.trigger)
        config = {"configurable": {"thread_id": paused.episode_id}}
        assert controller.checkpointer.get_tuple(config) is not None
        controller.expire(paused.episode_id)
        assert controller.checkpointer.get_tuple(config) is None


class Crashing:
    """An executor whose ``place`` dies like the process would: after the inner executor
    placed (and stored) the order, or before anything is sent. Only the first call dies."""

    def __init__(self, inner, *, after_placing: bool) -> None:
        self._inner = inner
        self.after_placing = after_placing
        self.calls = 0

    def place(self, intent):
        self.calls += 1
        if self.calls == 1 and not self.after_placing:
            raise RuntimeError("the process died before the order was sent")
        result = self._inner.place(intent)
        if self.calls == 1:
            raise RuntimeError("the process died before the order was recorded")
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def crashing(world, *, after_placing: bool):
    return dataclasses.replace(
        world, executor=Crashing(world.executor, after_placing=after_placing)
    )


def approval(paused, world) -> Review:
    return Review(
        proposal_id=paused.proposal.proposal_id,
        decision=ReviewDecision.APPROVE,
        decided_at=world.clock.now(),
    )


def test_an_approved_episode_that_died_after_sending_its_order_is_continued_without_a_second(
    tmp_path,
):
    file = fed_file()
    checkpoints = tmp_path / "checkpoints.sqlite"
    first_world = file_world(file, tmp_path, hawkish_script(), seed=True)
    saver = open_checkpointer(checkpoints)
    with EpisodeController(first_world, checkpointer=saver) as first:
        paused = first.run(file.trigger)
    saver.conn.close()
    first_world.store.close()  # type: ignore[attr-defined]

    second_world = crashing(
        file_world(file, tmp_path, hawkish_script(), seed=False), after_placing=True
    )
    advance(second_world, 60)
    saver = open_checkpointer(checkpoints)
    with (
        EpisodeController(second_world, checkpointer=saver) as second,
        pytest.raises(RuntimeError, match="died"),
    ):
        second.resume(paused.episode_id, approval(paused, second_world))
    saver.conn.close()
    second_world.store.close()  # type: ignore[attr-defined]

    third_world = file_world(file, tmp_path, hawkish_script(), seed=False)
    assert third_world.store.get_episode(paused.episode_id).status is EpisodeStatus.PENDING_REVIEW
    saver = open_checkpointer(checkpoints)
    with EpisodeController(third_world, checkpointer=saver) as third:
        record = third.resume(paused.episode_id, approval(paused, third_world))
    saver.conn.close()
    check_trace(record)
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    [order] = third_world.store.list_orders()
    assert record.orders == [order] and order.requested_contracts == paused.proposal.contracts


def test_an_approved_episode_that_died_before_sending_expires_if_continued_too_late():
    file = fed_file()
    world = crashing(
        make_world(file, hawkish_script(), policy=ReviewPolicy.PAUSE), after_placing=False
    )
    with EpisodeController(world) as controller:
        paused = controller.run(file.trigger)
        with pytest.raises(RuntimeError, match="died"):
            controller.resume(paused.episode_id, approval(paused, world))
        advance(world, 11 * 60)  # past the ten-minute expiry
        record = controller.expire(paused.episode_id)
    check_trace(record)
    assert record.exit is Exit.EXPIRED and world.store.list_orders() == []
    assert world.store.get_proposal(paused.proposal.proposal_id)[1] is ProposalStatus.EXPIRED


def test_running_a_trigger_again_continues_an_episode_that_died_at_execute():
    file = fed_file()
    world = crashing(make_world(file, hawkish_script()), after_placing=True)  # fill_tagged
    with EpisodeController(world) as controller:
        with pytest.raises(RuntimeError, match="died"):
            controller.run(file.trigger)
        assert world.store.list_episodes()[0].status is EpisodeStatus.RUNNING
        record = controller.run(file.trigger)
    check_trace(record)
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    assert len(world.store.list_orders()) == 1 and world.executor.calls == 1


def test_the_graph_is_drawn_as_mermaid():
    text = mermaid()
    assert text.startswith("---") or "graph" in text
    for node in ("match", "research", "forecast_step", "size", "gate", "route", "pause", "record"):
        assert node in text


def test_importing_the_controller_does_not_load_langgraph_or_mcp():
    code = (
        "import sys, pmfa.episode; "
        "print(any(m.split('.')[0] in ('langgraph', 'mcp') for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
