"""The controller around the graph: pause and resume across objects, idempotency, the docs graph."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from pmfa.episode import EpisodeController, episode_id_for, mermaid, open_checkpointer
from pmfa.episode.pipeline import client_order_id
from pmfa.errors import ExchangeError
from pmfa.records.decision import (
    ProposalStatus,
    ReasonCode,
    Review,
    ReviewDecision,
    ReviewPolicy,
)
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

    def find_order(self, intent):
        return self._inner.find_order(intent)

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


def rejection(paused, world) -> Review:
    return Review(
        proposal_id=paused.proposal.proposal_id,
        decision=ReviewDecision.REJECT,
        reason_code=ReasonCode.DISAGREE_WITH_FORECAST,
        decided_at=world.clock.now(),
    )


def approved_then_died(world):
    """Pause, approve, and die in ``place``: the thread stops at ``execute``."""
    controller = EpisodeController(world)
    paused = controller.run(world.file.trigger)
    with pytest.raises(RuntimeError, match="died"):
        controller.resume(paused.episode_id, approval(paused, world))
    assert world.executor.calls == 1
    return controller, paused


def continue_with(then: str, controller, world, paused):
    """Continue the stopped thread the way an operator would: ``then`` names the call."""
    if then == "approve":
        return controller.resume(paused.episode_id, approval(paused, world))
    if then == "reject":
        return controller.resume(paused.episode_id, rejection(paused, world))
    if then in ("expire", "expire_late"):
        return controller.expire(paused.episode_id)
    assert then == "run"
    return controller.run(world.file.trigger)


CONTINUATIONS = ["approve", "reject", "expire", "expire_late", "run"]


@pytest.mark.parametrize("then", CONTINUATIONS)
def test_a_continued_episode_that_died_before_sending_sends_nothing(then):
    """Whatever the operator does next, the stale approval is never acted on: the order
    was not sent, so the proposal is cancelled and the episode ends in error."""
    world = crashing(
        make_world(fed_file(), hawkish_script(), policy=ReviewPolicy.PAUSE), after_placing=False
    )
    controller, paused = approved_then_died(world)
    with controller:
        advance(world, 11 * 60 if then == "expire_late" else 60)
        record = continue_with(then, controller, world, paused)
        again = controller.resume(paused.episode_id, approval(paused, world))
    check_trace(record)
    assert world.executor.calls == 1  # the attempt that died; the continuation placed nothing
    assert record.exit is Exit.ERROR and "before the order was sent" in record.exit_detail
    assert record.status is EpisodeStatus.DONE and again == record
    assert world.store.list_orders() == [] and record.orders == []
    assert world.store.get_proposal(paused.proposal.proposal_id)[1] is ProposalStatus.CANCELLED


@pytest.mark.parametrize("then", CONTINUATIONS)
def test_a_continued_episode_that_died_after_sending_records_its_order_and_sends_no_other(then):
    """The order went out before the process died: a continuation records it (a reject or
    an expiry cannot unsend it) and places nothing more."""
    world = crashing(
        make_world(fed_file(), hawkish_script(), policy=ReviewPolicy.PAUSE), after_placing=True
    )
    controller, paused = approved_then_died(world)
    with controller:
        advance(world, 11 * 60 if then == "expire_late" else 60)
        record = continue_with(then, controller, world, paused)
    check_trace(record)
    assert world.executor.calls == 1
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    [order] = world.store.list_orders()
    assert record.orders == [order] and order.requested_contracts == paused.proposal.contracts
    assert world.store.get_proposal(paused.proposal.proposal_id)[1] is ProposalStatus.EXECUTED


class ExchangeThenDies:
    """Sends the order to a separate account standing in for the exchange, then dies
    before anything reaches the local store. ``find_order`` asks the exchange."""

    def __init__(self, inner, exchange, *, lookup_fails: bool = False) -> None:
        self._inner = inner
        self.exchange = exchange
        self.lookup_fails = lookup_fails
        self.calls = 0

    def place(self, intent):
        self.calls += 1
        self.exchange.place(intent)
        raise RuntimeError("the process died before the order was recorded")

    def find_order(self, intent):
        if self.lookup_fails:
            raise ExchangeError("GET /portfolio/orders -> 503 service_unavailable")
        return self.exchange.find_order(intent)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def exchange_world(*, lookup_fails: bool = False):
    from pmfa.execution.paper import PaperExecutor
    from pmfa.state.store import MEMORY, SqliteStore
    from pmfa.world.recorded import RecordedBooks

    world = make_world(fed_file(), hawkish_script(), policy=ReviewPolicy.PAUSE)
    exchange = PaperExecutor(
        SqliteStore(MEMORY), world.clock, world.config, world.exchange, RecordedBooks(world.file)
    )
    executor = ExchangeThenDies(world.executor, exchange, lookup_fails=lookup_fails)
    return dataclasses.replace(world, executor=executor)


@pytest.mark.parametrize("then", ["approve", "reject", "expire"])
def test_a_continuation_finds_an_order_only_the_exchange_holds_and_sends_no_other(then):
    world = exchange_world()
    controller, paused = approved_then_died(world)
    assert world.store.list_orders() == []  # only the exchange knows the order
    with controller:
        advance(world, 60)
        record = continue_with(then, controller, world, paused)
    check_trace(record)
    assert world.executor.calls == 1 and record.exit is Exit.EXECUTED
    [order] = world.store.list_orders()
    assert order == world.executor.exchange.store.list_orders()[0] and record.orders == [order]


def test_a_continuation_whose_order_lookup_fails_sends_nothing_and_says_the_outcome_is_unknown():
    world = exchange_world(lookup_fails=True)
    controller, paused = approved_then_died(world)
    with controller:
        advance(world, 60)
        record = controller.resume(paused.episode_id, approval(paused, world))
    check_trace(record)
    assert world.executor.calls == 1 and record.exit is Exit.ERROR
    assert "unknown" in record.exit_detail and "503" in record.exit_detail
    assert world.store.list_orders() == []


@pytest.mark.parametrize("then", ["approve", "run"])
def test_a_failed_lookup_does_not_cancel_the_proposal_as_never_sent(then):
    # The order was sent before the process died, and the lookup cannot reach the exchange:
    # the outcome is unknown, so the proposal must not be closed as if nothing was placed.
    world = exchange_world(lookup_fails=True)
    controller, paused = approved_then_died(world)
    with controller:
        advance(world, 60)
        record = continue_with(then, controller, world, paused)
    check_trace(record)
    assert world.executor.calls == 1 and record.exit is Exit.ERROR
    assert "unknown" in record.exit_detail and "nothing was placed" not in record.exit_detail
    status = world.store.get_proposal(paused.proposal.proposal_id)[1]
    assert status is not ProposalStatus.CANCELLED
    assert world.store.list_orders() == []


class Recording:
    """An executor that keeps every intent it is asked to place. ``find_order`` asks
    ``exchange``, a separate account standing in for the exchange's own record."""

    def __init__(self, inner, exchange) -> None:
        self._inner = inner
        self.exchange = exchange
        self.placed: list = []

    def place(self, intent):
        self.placed.append(intent)
        return self._inner.place(intent)

    def find_order(self, intent):
        return self.exchange.find_order(intent)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def recording_world():
    from pmfa.execution.paper import PaperExecutor
    from pmfa.state.store import MEMORY, SqliteStore
    from pmfa.world.recorded import RecordedBooks

    world = make_world(fed_file(), hawkish_script())  # fill_tagged: no pause before execute
    exchange = PaperExecutor(
        SqliteStore(MEMORY), world.clock, world.config, world.exchange, RecordedBooks(world.file)
    )
    return dataclasses.replace(world, executor=Recording(world.executor, exchange))


def test_a_first_run_finds_an_order_already_placed_under_its_client_order_id_and_sends_no_other():
    """The client order id is a function of the episode and its proposal, so the same
    trigger always builds the same one. When the exchange already holds an order under
    it (a run that sent the order and lost every local trace of it), a first run of
    execute records that order instead of sending another."""
    first_world = recording_world()
    with EpisodeController(first_world) as controller:
        first = controller.run(first_world.file.trigger)
    assert first.exit is Exit.EXECUTED and first.proposal is not None
    [sent] = first_world.executor.placed
    assert sent.client_order_id == client_order_id(first.episode_id, first.proposal)

    # The same trigger in a fresh world builds the same client order id.
    again_world = recording_world()
    with EpisodeController(again_world) as controller:
        controller.run(again_world.file.trigger)
    [resent] = again_world.executor.placed
    assert resent.client_order_id == sent.client_order_id

    # A fresh world whose exchange already holds that order: nothing new is sent.
    world = recording_world()
    standing = world.executor.exchange.place(sent)
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
    check_trace(record)
    assert world.executor.placed == []
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    assert world.store.list_orders() == [standing] and record.orders == [standing]
    assert world.store.get_proposal(record.proposal.proposal_id)[1] is ProposalStatus.EXECUTED


@pytest.mark.parametrize("after_placing", [True, False])
def test_running_a_trigger_again_continues_an_episode_that_died_at_execute(after_placing):
    file = fed_file()
    world = crashing(make_world(file, hawkish_script()), after_placing=after_placing)
    with EpisodeController(world) as controller:  # fill_tagged: no pause before execute
        with pytest.raises(RuntimeError, match="died"):
            controller.run(file.trigger)
        assert world.store.list_episodes()[0].status is EpisodeStatus.RUNNING
        record = controller.run(file.trigger)
    check_trace(record)
    assert record.status is EpisodeStatus.DONE and world.executor.calls == 1
    if after_placing:
        assert record.exit is Exit.EXECUTED and len(world.store.list_orders()) == 1
    else:
        assert record.exit is Exit.ERROR and world.store.list_orders() == []


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
