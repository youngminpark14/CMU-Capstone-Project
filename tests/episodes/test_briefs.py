"""What a role sees of the trigger: a price move reaches the roles with no number.

No role may see the price of a market it forecasts. A price-move trigger's
detail names the move in prices, so these tests put distinctive prices in the
detail and check that none of them reaches the brief, or any role's input.
"""

from __future__ import annotations

from datetime import timedelta

from pmfa.episode import EpisodeController
from pmfa.episode.pipeline import trigger_brief
from pmfa.records.evidence import Trigger, TriggerKind

from .support import (
    AS_OF,
    FED,
    H0,
    MINUTES,
    PHANTOM_PRICES,
    SPEECH,
    books_for,
    check_trace,
    fed_event,
    fed_file,
    hawkish_script,
    make_world,
)

PRICES = ("0.37", "0.4142", "4.4c")
"""The numbers in the detail. None of them appears in the fixtures' prices or texts."""

DETAIL = f"{H0} YES mid moved 4.4c within 30 min (at 15:05 UTC), from 0.37 to 0.4142"


def leaked(text: str) -> list[str]:
    """The detail's numbers that appear in ``text``."""
    return [p for p in PRICES if p in text]


def test_a_price_move_brief_carries_none_of_the_prices_in_its_trigger():
    move = Trigger(
        trigger_id="move-brief",
        kind=TriggerKind.PRICE_MOVE,
        at=AS_OF,
        event_ticker=FED,
        detail=DETAIL,
    )
    brief = trigger_brief(move, None)
    assert brief.kind == TriggerKind.PRICE_MOVE.value
    assert leaked(brief.model_dump_json()) == []


def test_a_scheduled_re_check_brief_carries_nothing_from_its_trigger_detail():
    tick = Trigger(
        trigger_id="tick-brief",
        kind=TriggerKind.SCHEDULE,
        at=AS_OF,
        event_ticker=FED,
        detail=DETAIL,
    )
    assert leaked(trigger_brief(tick, None).model_dump_json()) == []


def test_no_role_in_a_price_move_episode_is_shown_the_prices_in_its_trigger():
    later = AS_OF + timedelta(hours=1)
    fresh = SPEECH.model_copy(
        update={"doc_id": "speech-fresh", "published_at": later - timedelta(minutes=5)}
    )
    event = fed_event(prices=PHANTOM_PRICES)
    file = fed_file(event=event, order_books=books_for(event), documents=[MINUTES, SPEECH, fresh])
    world = make_world(file, hawkish_script())
    with EpisodeController(world) as controller:
        controller.run(file.trigger)
        world.clock.set(later)  # type: ignore[attr-defined]
        move = Trigger(
            trigger_id="move-roles",
            kind=TriggerKind.PRICE_MOVE,
            at=later,
            event_ticker=FED,
            detail=DETAIL,
        )
        record = controller.run(move)
    check_trace(record)
    assert record.forecast is not None  # the roles were called on the price move
    calls = [c for c in world.models.calls if c.episode_id == record.episode_id]
    assert calls
    assert [c.task for c in calls if leaked(c.payload.model_dump_json())] == []
