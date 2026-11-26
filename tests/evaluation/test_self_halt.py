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


# Listed out of order on purpose: the pass ran them by start time.
TRADING = [
    episode(4, 4, Exit.HALTED),
    episode(1, 1, Exit.EXECUTED),
    episode(3, 3, Exit.NO_EDGE),
    episode(2, 2, Exit.HALTED),
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


def test_the_first_halt_is_the_earliest_to_start_not_the_earliest_as_of_or_id() -> None:
    """With a late start, an episode's as-of time and id can sort before one that ran first.

    ``ep-0`` has the earliest as-of time and the smallest id, but the pass
    started it at hour 3.5, after ``ep-3``; ``ep-2`` halted first by start time.
    """
    late = episode(0, 0.5, Exit.HALTED).model_copy(
        update={"started_at": T0 + timedelta(hours=3.5), "exit_detail": "gate: halt"}
    )
    found = self_halt([*TRADING, late])
    assert found is not None
    assert (found.first_episode_id, found.reason) == ("ep-2", FILL_STOP)
    # From ep-2 on by start time: ep-2, ep-3, ep-0, ep-4, ep-5; all but ep-3 halted.
    assert (found.n_from_then, found.n_halted_from_then) == (5, 4)


def test_episodes_that_share_a_time_are_taken_in_the_order_the_pass_ran_them() -> None:
    """Two scoring points at one as-of time run by event ticker; ids are hashes and may not.

    The ids here sort the other way on purpose. KXCPIYOY ran first, tripped
    the drawdown stop, and KXFEDDECISION then met the halt flag at the gate.
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
        point("aaa", "KXFEDDECISION-26AUG", Exit.HALTED, "gate: halt"),
        point("zzz", "KXCPIYOY-26AUG", Exit.HALTED, drawdown),
    ]
    found = self_halt(both_halted)
    assert found is not None
    assert (found.first_trigger_id, found.reason) == ("schedule:KXCPIYOY-26AUG:7d", drawdown)
    assert (found.n_from_then, found.n_halted_from_then) == (2, 2)

    executed_then_halted = [
        point("aaa", "KXFEDDECISION-26AUG", Exit.HALTED, drawdown),
        point("zzz", "KXCPIYOY-26AUG", Exit.EXECUTED, ""),
    ]
    found = self_halt(executed_then_halted)
    assert found is not None
    assert found.first_trigger_id == "schedule:KXFEDDECISION-26AUG:7d"
    assert (found.n_from_then, found.n_halted_from_then) == (1, 1)

    trading = [
        Trigger(trigger_id=f"doc:{d}", kind=TriggerKind.NEW_DOCUMENT, at=at, doc_id=d)
        for d in ("doc-b", "doc-a")
    ]
    docs = [
        EpisodeRecord(
            episode_id=eid,
            mode=RunMode.BACKTEST,
            trigger=t,
            as_of=at,
            exit=Exit.HALTED,
            exit_detail=detail,
            started_at=at,
        )
        for eid, t, detail in (("aaa", trading[0], "gate: halt"), ("zzz", trading[1], FILL_STOP))
    ]
    found = self_halt(docs)
    assert found is not None
    assert (found.first_trigger_id, found.reason) == ("doc:doc-a", FILL_STOP)
