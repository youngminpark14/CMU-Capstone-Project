"""An order whose outcome is unknown: the episode waits ``OUTCOME_UNKNOWN`` and is reconciled.

Sending raised an exchange error and the order could not be found, so it may stand on
the exchange. Nothing may say it was refused, filled or never placed, and nothing may
send it again: every later call only looks it up by its client order id.
"""

from __future__ import annotations

import pytest

from pmfa.episode import EpisodeController, NotPaused, open_checkpointer
from pmfa.episode.pipeline import client_order_id
from pmfa.execution.review import ReviewQueue, ReviewRefused
from pmfa.records.decision import (
    OrderStatus,
    ProposalStatus,
    ReasonCode,
    Review,
    ReviewDecision,
    ReviewPolicy,
)
from pmfa.records.episode import EpisodeStatus, Exit

from .support import (
    AS_OF,
    advance,
    check_trace,
    doubtful,
    exchange_account,
    fed_file,
    file_world,
    hawkish_script,
    make_world,
)


def world_in_doubt(*, policy: ReviewPolicy = ReviewPolicy.FILL_TAGGED, **settings):
    return doubtful(make_world(fed_file(), hawkish_script(), policy=policy), **settings)


def proposal_status(world, record) -> ProposalStatus:
    return world.store.get_proposal(record.proposal.proposal_id)[1]


def has_thread(controller: EpisodeController, episode_id: str) -> bool:
    return (
        controller.checkpointer.get_tuple({"configurable": {"thread_id": episode_id}}) is not None
    )


def assert_unknown(world, controller, record) -> None:
    """The episode waits: no exit, nothing stored as an order, the proposal in doubt too."""
    check_trace(record)
    assert record.status is EpisodeStatus.OUTCOME_UNKNOWN and record.exit is None
    assert record.exit_detail == "" and record.finished_at is None
    assert world.store.get_episode(record.episode_id) == record
    assert proposal_status(world, record) is ProposalStatus.OUTCOME_UNKNOWN
    assert world.store.list_orders() == [] and record.orders == []
    assert has_thread(controller, record.episode_id)


@pytest.mark.parametrize(
    ("accept", "failures"),
    [(True, 1), (False, 0)],
    ids=["the lookup fails", "the lookup lists no order yet"],
)
def test_a_send_whose_answer_is_lost_and_whose_order_is_not_found_waits_with_the_outcome_unknown(
    accept, failures
):
    world = world_in_doubt(accept=accept, failures=failures)
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
        assert_unknown(world, controller, record)
    [sent] = world.executor.placed
    assert sent.client_order_id == client_order_id(record.episode_id, record.proposal)
    said = record.trace[-1]
    assert said.node == "execute" and sent.client_order_id in said.data["detail"]
    assert "504" in said.data["detail"]


def test_a_reconcile_that_finds_the_filled_order_records_it_and_ends_the_episode_executed():
    world = world_in_doubt(failures=1)
    with EpisodeController(world) as controller:
        waiting = controller.run(world.file.trigger)
        record = controller.reconcile(waiting.episode_id)
        again = controller.reconcile(waiting.episode_id)
        assert not has_thread(controller, record.episode_id)
    check_trace(record)
    assert record.status is EpisodeStatus.DONE and record.exit is Exit.EXECUTED
    [order] = world.store.list_orders()
    assert order == world.executor.exchange.find_order(world.executor.placed[0])
    assert order.status is OrderStatus.FILLED and record.orders == [order]
    assert proposal_status(world, record) is ProposalStatus.EXECUTED
    assert len(world.executor.placed) == 1
    assert again == record  # a finished episode is returned unchanged
    traded = [e for e in world.store.ledger() if e.market_ticker == order.market_ticker]
    assert traded and traded[0].traded


def test_a_reconcile_the_exchange_answers_with_no_order_cancels_the_proposal_and_ends_in_error():
    world = world_in_doubt(accept=False, failures=1)
    with EpisodeController(world) as controller:
        waiting = controller.run(world.file.trigger)
        record = controller.reconcile(waiting.episode_id)
    check_trace(record)
    assert record.status is EpisodeStatus.DONE and record.exit is Exit.ERROR
    [sent] = world.executor.placed
    assert sent.client_order_id in record.exit_detail
    assert "nothing was placed" in record.exit_detail
    assert proposal_status(world, record) is ProposalStatus.CANCELLED
    assert world.store.list_orders() == []


def test_a_lookup_that_keeps_failing_keeps_waiting_and_never_sends_again():
    world = world_in_doubt(failures=4)  # the lookup in execute, then three reconciles
    with EpisodeController(world) as controller:
        record = controller.run(world.file.trigger)
        for _ in range(3):
            record = controller.reconcile(record.episode_id)
            assert_unknown(world, controller, record)
            assert len(world.executor.placed) == 1
        record = controller.reconcile(record.episode_id)
    check_trace(record)
    assert record.exit is Exit.EXECUTED and len(world.executor.placed) == 1
    assert proposal_status(world, record) is ProposalStatus.EXECUTED
    assert [o.order_id for o in record.orders] == [o.order_id for o in world.store.list_orders()]


def paused_then_unknown(world, controller):
    """Pause for review, approve, and lose the answer to the order: the episode waits."""
    paused = controller.run(world.file.trigger)
    assert paused.status is EpisodeStatus.PENDING_REVIEW
    record = controller.resume(paused.episode_id, review(paused, world, ReviewDecision.APPROVE))
    assert_unknown(world, controller, record)
    return paused


def review(paused, world, decision: ReviewDecision) -> Review:
    extra = {}
    if decision is ReviewDecision.RESIZE:
        extra = {"contracts": 1, "reason_code": ReasonCode.SIZE_TOO_LARGE}
    if decision is ReviewDecision.REJECT:
        extra = {"reason_code": ReasonCode.DISAGREE_WITH_FORECAST}
    return Review(
        proposal_id=paused.proposal.proposal_id,
        decision=decision,
        decided_at=world.clock.now(),
        **extra,
    )


CALLS = ["run", "approve", "resize", "reject", "expire", "expire_late", "reconcile"]


def ask_again(then: str, controller, world, paused):
    """Reach the waiting episode the way an operator or the live loop would."""
    if then == "run":
        return controller.run(world.file.trigger)
    if then in ("approve", "resize", "reject"):
        return controller.resume(paused.episode_id, review(paused, world, ReviewDecision(then)))
    if then in ("expire", "expire_late"):
        return controller.expire(paused.episode_id)
    return controller.reconcile(paused.episode_id)


@pytest.mark.parametrize("then", CALLS)
def test_every_call_on_an_episode_in_doubt_only_reconciles_and_never_places(then):
    world = world_in_doubt(policy=ReviewPolicy.PAUSE, failures=1)
    with EpisodeController(world) as controller:
        paused = paused_then_unknown(world, controller)
        advance(world, 11 * 60 if then == "expire_late" else 60)
        record = ask_again(then, controller, world, paused)
    check_trace(record)
    assert len(world.executor.placed) == 1
    assert record.exit is Exit.EXECUTED and record.status is EpisodeStatus.DONE
    [order] = world.store.list_orders()
    assert order.requested_contracts == paused.proposal.contracts  # a resize changed nothing
    assert proposal_status(world, record) is ProposalStatus.EXECUTED
    assert len(world.store.list_reviews()) == 1  # only the approval that sent the order


@pytest.mark.parametrize("then", CALLS)
def test_every_call_while_the_exchange_still_cannot_answer_keeps_waiting(then):
    world = world_in_doubt(policy=ReviewPolicy.PAUSE, failures=3)
    with EpisodeController(world) as controller:
        paused = paused_then_unknown(world, controller)
        advance(world, 11 * 60 if then == "expire_late" else 60)
        record = ask_again(then, controller, world, paused)
        assert_unknown(world, controller, record)
        again = ask_again(then, controller, world, paused)
        assert_unknown(world, controller, again)
    assert len(world.executor.placed) == 1


def test_the_review_queue_refuses_to_decide_or_expire_a_proposal_in_doubt():
    world = world_in_doubt(policy=ReviewPolicy.PAUSE, failures=1)
    with EpisodeController(world) as controller:
        paused = paused_then_unknown(world, controller)
    queue = ReviewQueue(world.store, world.clock, world.config)
    pid = paused.proposal.proposal_id
    assert queue.pending() == []
    with pytest.raises(ReviewRefused, match="outcome_unknown"):
        queue.decide(pid, ReviewDecision.REJECT, reason_code=ReasonCode.OTHER)
    with pytest.raises(ReviewRefused, match="outcome_unknown"):
        queue.expire(pid)
    assert queue.expire_due() == []
    assert world.store.get_proposal(pid)[1] is ProposalStatus.OUTCOME_UNKNOWN


def test_the_halt_flag_does_not_stop_a_reconcile_from_recording_the_order_it_finds():
    world = world_in_doubt(failures=1)
    with EpisodeController(world) as controller:
        waiting = controller.run(world.file.trigger)
        world.store.set_halt("manual", AS_OF)
        record = controller.reconcile(waiting.episode_id)
    assert record.exit is Exit.EXECUTED and len(world.executor.placed) == 1
    assert len(world.store.list_orders()) == 1
    assert proposal_status(world, record) is ProposalStatus.EXECUTED


def test_reconcile_returns_any_other_episode_unchanged():
    world = make_world(fed_file(), hawkish_script(), policy=ReviewPolicy.PAUSE)
    with EpisodeController(world) as controller:
        paused = controller.run(world.file.trigger)
        assert controller.reconcile(paused.episode_id) == paused
        assert has_thread(controller, paused.episode_id)


def test_an_episode_in_doubt_with_no_thread_here_is_not_reconciled_and_never_rerun():
    world = world_in_doubt(failures=1)
    with EpisodeController(world) as controller:
        waiting = controller.run(world.file.trigger)
    with EpisodeController(world) as fresh:  # a new, empty in-memory checkpointer
        coid = world.executor.placed[0].client_order_id
        with pytest.raises(NotPaused, match=f"checkpoint is missing.*client order id {coid}"):
            fresh.reconcile(waiting.episode_id)
        assert fresh.run(world.file.trigger) == waiting
    assert len(world.executor.placed) == 1


# --------------------------------------------------------------------------- across processes


def files_in_doubt(tmp_path, exchange=None, placed=None, *, failures: int, seed: bool):
    world = file_world(
        fed_file(), tmp_path, hawkish_script(), policy=ReviewPolicy.FILL_TAGGED, seed=seed
    )
    return doubtful(
        world,
        exchange if exchange is not None else exchange_account(world),
        failures=failures,
        placed=placed,
    )


def test_a_second_controller_over_the_same_files_reconciles_what_the_first_left_unknown(tmp_path):
    checkpoints = tmp_path / "checkpoints.sqlite"
    first_world = files_in_doubt(tmp_path, failures=1, seed=True)
    saver = open_checkpointer(checkpoints)
    with EpisodeController(first_world, checkpointer=saver) as first:
        waiting = first.run(first_world.file.trigger)
        assert_unknown(first_world, first, waiting)
    saver.conn.close()
    first_world.store.close()  # type: ignore[attr-defined]

    exchange, placed = first_world.executor.exchange, first_world.executor.placed
    second_world = files_in_doubt(tmp_path, exchange, placed, failures=0, seed=False)
    advance(second_world, 300)
    saver = open_checkpointer(checkpoints)
    with EpisodeController(second_world, checkpointer=saver) as second:
        record = second.reconcile(waiting.episode_id)
    saver.conn.close()
    check_trace(record)
    assert record.exit is Exit.EXECUTED and len(placed) == 1
    [order] = second_world.store.list_orders()
    assert record.orders == [order] and order == exchange.find_order(placed[0])
    # The trace goes on where the first process left it, numbered without gaps.
    assert record.trace[: len(waiting.trace)] == waiting.trace


@pytest.mark.parametrize("then", ["reconcile", "run"])
def test_a_process_that_died_inside_reconcile_is_continued_without_a_second_order(tmp_path, then):
    checkpoints = tmp_path / "checkpoints.sqlite"
    first_world = files_in_doubt(tmp_path, failures=1, seed=True)
    saver = open_checkpointer(checkpoints)
    with EpisodeController(first_world, checkpointer=saver) as first:
        waiting = first.run(first_world.file.trigger)
        first_world.executor.crash = True
        with pytest.raises(RuntimeError, match="died"):
            first.reconcile(waiting.episode_id)
        # The wait was resumed and the process died in reconcile: no interrupt, next reconcile.
        snapshot = first.graph.get_state(first._config(waiting.episode_id))
        assert tuple(snapshot.next) == ("reconcile",) and not snapshot.interrupts
    saver.conn.close()
    first_world.store.close()  # type: ignore[attr-defined]

    exchange, placed = first_world.executor.exchange, first_world.executor.placed
    second_world = files_in_doubt(tmp_path, exchange, placed, failures=0, seed=False)
    assert second_world.store.get_episode(waiting.episode_id).status is (
        EpisodeStatus.OUTCOME_UNKNOWN
    )
    saver = open_checkpointer(checkpoints)
    with EpisodeController(second_world, checkpointer=saver) as second:
        if then == "run":
            record = second.run(second_world.file.trigger)
        else:
            record = second.reconcile(waiting.episode_id)
    saver.conn.close()
    check_trace(record)
    assert record.exit is Exit.EXECUTED and len(placed) == 1
    assert len(second_world.store.list_orders()) == 1


@pytest.mark.parametrize("found", [True, False], ids=["then found", "lookup fails again"])
def test_a_process_that_died_in_the_lookup_after_sending_is_continued_without_a_second_order(
    found,
):
    world = world_in_doubt(failures=0 if found else 1)
    world.executor.crash = True  # the one lookup after the failed send dies
    with EpisodeController(world) as controller:
        with pytest.raises(RuntimeError, match="died"):
            controller.run(world.file.trigger)
        assert world.store.list_episodes()[0].status is EpisodeStatus.RUNNING
        record = controller.run(world.file.trigger)
        if not found:
            assert_unknown(world, controller, record)
            record = controller.reconcile(record.episode_id)
    check_trace(record)
    assert record.exit is Exit.EXECUTED and len(world.executor.placed) == 1
