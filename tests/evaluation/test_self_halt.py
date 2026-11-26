"""The first self-halt of each pass, its reason, and how many later episodes ended halted."""

from __future__ import annotations

from datetime import timedelta

from pmfa.config import Config
from pmfa.evaluation.run_metrics import compute_metrics
from pmfa.evaluation.summary import self_halt
from pmfa.records.base import RunMode
from pmfa.records.episode import EpisodeRecord, Exit
from pmfa.records.evidence import Trigger, TriggerKind

from .factories import T0

FILL_STOP = "self-halt: fill failures on 2 of 10 orders"


def episode(
    n: int, hours: float, exit: Exit, *, kind: TriggerKind = TriggerKind.NEW_DOCUMENT
) -> EpisodeRecord:
    at = T0 + timedelta(hours=hours)
    return EpisodeRecord(
        episode_id=f"ep-{n}",
        mode=RunMode.BACKTEST,
        trigger=Trigger(trigger_id=f"tr-{n}", kind=kind, at=at, event_ticker="EV"),
        as_of=at,
        exit=exit,
        exit_detail=FILL_STOP if exit is Exit.HALTED else "",
        started_at=at,
    )


# In the order the pass ran them, which is the order ``self_halt`` takes them in.
TRADING = [
    episode(1, 1, Exit.EXECUTED),
    episode(2, 2, Exit.HALTED),
    episode(3, 3, Exit.NO_EDGE),
    episode(4, 4, Exit.HALTED),
    episode(5, 5, Exit.HALTED),
]


def test_the_first_halt_and_what_followed_it() -> None:
    found = self_halt(TRADING)
    assert found is not None
    assert (found.first_episode_id, found.first_trigger_id) == ("ep-2", "tr-2")
    assert found.first_started_at == T0 + timedelta(hours=2)
    assert found.reason == FILL_STOP
    # From ep-2 on: ep-2, ep-3, ep-4, ep-5; all but ep-3 ended halted.
    assert (found.n_from_then, found.n_halted_from_then) == (4, 3)


def test_no_halt_gives_nothing() -> None:
    assert self_halt([episode(1, 1, Exit.EXECUTED), episode(2, 2, Exit.NO_EDGE)]) is None
    assert self_halt([]) is None


def test_the_passes_are_told_apart_by_trigger_kind() -> None:
    scoring = [
        episode(10, 0, Exit.NO_EDGE, kind=TriggerKind.SCHEDULE),
        episode(11, 6, Exit.NO_EDGE, kind=TriggerKind.SCHEDULE),
    ]
    metrics = compute_metrics([], [], [], [*scoring, *TRADING], [], [], systems={}, config=Config())
    assert metrics["self_halts"]["scoring"] is None
    trading = metrics["self_halts"]["trading"]
    assert trading["first_episode_id"] == "ep-2" and trading["reason"] == FILL_STOP
    assert (trading["n_from_then"], trading["n_halted_from_then"]) == (4, 3)


def test_the_first_halt_is_the_first_in_run_order_not_the_earliest_as_of_or_id() -> None:
    """An episode that ran later can have an earlier as-of time and a smaller id.

    ``ep-0`` ran after ``ep-3`` but has the earliest as-of time and the
    smallest id; ``ep-2`` halted first in the order the pass ran them.
    """
    late = episode(0, 0.5, Exit.HALTED).model_copy(
        update={"started_at": T0 + timedelta(hours=3.5), "exit_detail": "gate: halt"}
    )
    ran = [*TRADING[:3], late, *TRADING[3:]]
    found = self_halt(ran)
    assert found is not None
    assert (found.first_episode_id, found.reason) == ("ep-2", FILL_STOP)
    # From ep-2 on in run order: ep-2, ep-3, ep-0, ep-4, ep-5; all but ep-3 halted.
    assert (found.n_from_then, found.n_halted_from_then) == (5, 4)


def test_scoring_points_that_share_a_time_are_taken_in_the_order_given() -> None:
    """Two scoring points at one as-of time ran by event ticker; their ids sort the other way.

    KXCPIYOY ran first and tripped the drawdown stop; KXFEDDECISION then met
    the halt flag at the gate.
    """
    at = T0 + timedelta(days=1)

    def point(episode_id: str, event: str, exit: Exit, detail: str) -> EpisodeRecord:
        return EpisodeRecord(
            episode_id=episode_id,
            mode=RunMode.BACKTEST,
            trigger=Trigger(
                trigger_id=f"schedule:{event}:7d",
                kind=TriggerKind.SCHEDULE,
                at=at,
                event_ticker=event,
            ),
            as_of=at,
            exit=exit,
            exit_detail=detail,
            started_at=at,
        )

    drawdown = "self-halt: drawdown 21% exceeds 20%"
    both_halted = [
        point("zzz", "KXCPIYOY-26AUG", Exit.HALTED, drawdown),
        point("aaa", "KXFEDDECISION-26AUG", Exit.HALTED, "gate: halt"),
    ]
    found = self_halt(both_halted)
    assert found is not None
    assert (found.first_trigger_id, found.reason) == ("schedule:KXCPIYOY-26AUG:7d", drawdown)
    assert (found.n_from_then, found.n_halted_from_then) == (2, 2)

    executed_then_halted = [
        point("zzz", "KXCPIYOY-26AUG", Exit.EXECUTED, ""),
        point("aaa", "KXFEDDECISION-26AUG", Exit.HALTED, drawdown),
    ]
    found = self_halt(executed_then_halted)
    assert found is not None
    assert found.first_trigger_id == "schedule:KXFEDDECISION-26AUG:7d"
    assert (found.n_from_then, found.n_halted_from_then) == (1, 1)


def test_two_late_trading_episodes_that_share_a_start_are_taken_in_run_order() -> None:
    """Two documents waited for the same order outcome, so both started, and are as of, then.

    The pass ran them in the schedule's order, by publication time: ``doc-b``
    was published first, though its id sorts after ``doc-a``. ``doc-b``
    tripped the stop; ``doc-a`` then met the halt flag at the gate. The block
    must name ``doc-b`` with its own reason, not the flag ``doc-a`` met.
    """
    start = T0 + timedelta(minutes=2)

    def late(doc_id: str, episode_id: str, detail: str) -> EpisodeRecord:
        # A late episode runs on a copy of its trigger with ``at`` moved to its start.
        trigger = Trigger(
            trigger_id=f"doc:{doc_id}", kind=TriggerKind.NEW_DOCUMENT, at=start, doc_id=doc_id
        )
        return EpisodeRecord(
            episode_id=episode_id,
            mode=RunMode.BACKTEST,
            trigger=trigger,
            as_of=start,
            exit=Exit.HALTED,
            exit_detail=detail,
            started_at=start,
        )

    ran = [late("doc-b", "zzz", FILL_STOP), late("doc-a", "aaa", "gate: halt")]
    found = self_halt(ran)
    assert found is not None
    assert (found.first_trigger_id, found.first_episode_id) == ("doc:doc-b", "zzz")
    assert found.reason == FILL_STOP
    assert (found.n_from_then, found.n_halted_from_then) == (2, 2)

    # The metrics keep the order they are given in, too.
    metrics = compute_metrics([], [], [], ran, [], [], systems={}, config=Config())
    trading = metrics["self_halts"]["trading"]
    assert (trading["first_trigger_id"], trading["reason"]) == ("doc:doc-b", FILL_STOP)
