"""The controller around the graph: pause and resume across objects, idempotency, the docs graph."""

from __future__ import annotations

import subprocess
import sys

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
