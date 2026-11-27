"""A world rebuilt from a recording's file starts where the recorded world stood.

Each test puts a history into a paper world's store, records the world at the
as-of time, rebuilds a world from the recorded file, and compares what the two
answer at that time: the balance and positions sizing reads, the risk state, and
the self-halt conditions. A trace saved late in a trading simulation replays to
the same decision only if these agree.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pmfa.decision.risk import risk_state
from pmfa.errors import ConfigError
from pmfa.execution.halt import stop_reasons
from pmfa.records.decision import Fill, OrderResult, OrderStatus, ReviewPolicy
from pmfa.records.forecast import Forecast
from pmfa.records.market import Side
from pmfa.records.roles import RejectionBrief
from pmfa.world.recorded import (
    RecordedRejection,
    RecordedWorld,
    RecordedWorldFile,
    build_recorded_world,
    first_equity,
)
from pmfa.world.recording import Recording

from .factories import AS_OF, CUT, EVENT, HOLD, NoModels, prior_fill, world_file

EXPECTED_FIRST_EQUITY = Decimal("1000.01")
"""The factory's account with ``prior_fill`` marked at the trigger: 1000 less 5 x 0.60 and the
0.09 fee is 996.91 in cash, and the five NO contracts of the cut market are worth its NO bid,
1 - 0.38 = 0.62, each (the market has no book, so its quote makes one): 3.10."""


def _order(
    order_id: str,
    placed_at: datetime,
    *,
    filled: int = 0,
    market: str = HOLD,
    side: Side = Side.YES,
    price: str = "0.62",
    outcome_at: datetime | None = None,
) -> OrderResult:
    """A prior order: ``filled`` contracts at ``price``, or unfilled when ``filled`` is 0."""
    fills = (
        [
            Fill(
                fill_id=f"{order_id}-fill",
                order_id=order_id,
                market_ticker=market,
                side=side,
                contracts=Decimal(filled),
                price=price,
                fee="0.10",
                filled_at=outcome_at or placed_at,
                simulated=True,
            )
        ]
        if filled
        else []
    )
    return OrderResult(
        order_id=order_id,
        client_order_id=order_id,
        episode_id="ep-earlier",
        market_ticker=market,
        side=side,
        requested_contracts=max(filled, 1),
        limit_price=price,
        status=OrderStatus.FILLED if filled else OrderStatus.UNFILLED,
        fills=fills,
        placed_at=placed_at,
        outcome_at=outcome_at,
    )


def _world(file: RecordedWorldFile | None = None) -> RecordedWorld:
    return build_recorded_world(
        file or world_file(), models=NoModels(), review_policy=ReviewPolicy.FILL_TAGGED
    )


def _rebuilt(world: RecordedWorld) -> RecordedWorld:
    """Record ``world`` as it stands at its clock's time, and build a world from the file."""
    recording = Recording(world)
    return _world(recording.file(world.file.trigger, description="round trip"))


def _answers(world: RecordedWorld) -> dict:
    """What the episode's sizing, gate and stop conditions read from a world at its clock's time."""
    now = world.clock.now()
    balance = world.executor.balance()
    return {
        "balance": balance,
        "positions": world.executor.positions(),
        "risk": risk_state(world.store.equity_series(), now, world.config, bankroll=balance.equity),
        "stops": stop_reasons(world.store, world.config, world.clock, world.evidence),
    }


def test_settled_holdings_are_paid_out_and_closed_in_the_rebuilt_world():
    world = _world()
    days_ago = AS_OF - timedelta(days=5)
    # YES on the hold market settles NO (a loss); NO on the cut market settles NO (a win).
    world.store.save_order(_order("lost", days_ago, filled=10, market=HOLD, price="0.60"))
    world.store.save_order(
        _order("won", days_ago, filled=5, market=CUT, side=Side.NO, price="0.62")
    )
    world.store.record_settlement(HOLD, Side.NO, AS_OF - timedelta(days=1))
    world.store.record_settlement(CUT, Side.NO, AS_OF - timedelta(days=1))

    rebuilt = _rebuilt(world)

    assert world.executor.positions() == []
    assert world.executor.balance().cash == Decimal("1000") - Decimal("6.10") - Decimal("3.20") + 5
    assert rebuilt.executor.balance() == world.executor.balance()
    assert rebuilt.executor.positions() == world.executor.positions()
    assert _answers(rebuilt) == _answers(world)


def test_prior_orders_that_did_not_fill_count_toward_the_fill_failure_stop_in_the_rebuilt_world():
    world = _world()
    for i in range(10):
        placed = AS_OF - timedelta(days=2, minutes=10 - i)
        world.store.save_order(_order(f"o{i}", placed, filled=0 if i in (3, 7) else 1))

    rebuilt = _rebuilt(world)

    reasons = stop_reasons(world.store, world.config, world.clock, world.evidence)
    assert "fill failures: 2 of the last 10 orders did not fill" in reasons
    assert stop_reasons(rebuilt.store, rebuilt.config, rebuilt.clock, rebuilt.evidence) == reasons
    assert _answers(rebuilt) == _answers(world)


def test_a_drawdown_from_an_earlier_peak_halts_the_rebuilt_world_too():
    world = _world()
    store = world.store
    store.record_equity(AS_OF - timedelta(days=4), Decimal("1000"))
    store.record_equity(AS_OF - timedelta(days=3), Decimal("1200"))
    # The fall was over by yesterday, so today's loss stop does not hold.
    store.record_equity(AS_OF - timedelta(days=1), Decimal("1000"))
    store.record_equity(AS_OF, Decimal("1000"))  # 1/6 below the peak; the halt is at 0.10

    rebuilt = _rebuilt(world)

    reasons = stop_reasons(store, world.config, world.clock, world.evidence)
    assert any(r.startswith("drawdown") for r in reasons)
    assert not any(r.startswith("daily loss stop") for r in reasons)
    assert stop_reasons(rebuilt.store, rebuilt.config, rebuilt.clock, rebuilt.evidence) == reasons
    assert _answers(rebuilt) == _answers(world)


def test_a_fall_earlier_today_trips_the_daily_loss_stop_in_the_rebuilt_world_too():
    world = _world()
    store = world.store
    store.record_equity(AS_OF - timedelta(days=1), Decimal("1000"))
    # 15:00 UTC is 10:00 in New York: both points below are in the same trading day.
    store.record_equity(AS_OF - timedelta(hours=3), Decimal("960"))  # 4% below the day's start
    store.record_equity(AS_OF, Decimal("995"))  # recovered since, but the stop stays tripped

    rebuilt = _rebuilt(world)

    reasons = stop_reasons(store, world.config, world.clock, world.evidence)
    assert any(r.startswith("daily loss stop") for r in reasons)
    assert not any(r.startswith("drawdown") for r in reasons)
    assert stop_reasons(rebuilt.store, rebuilt.config, rebuilt.clock, rebuilt.evidence) == reasons
    assert _answers(rebuilt) == _answers(world)


def test_the_fill_failure_stop_reads_the_same_last_orders_in_send_order_in_the_rebuilt_world():
    world = _world()
    start = AS_OF - timedelta(hours=2)
    # Sent first, but priced from an hourly candle: its outcome is later than every other's.
    world.store.save_order(_order("o0", start, filled=1, outcome_at=start + timedelta(minutes=110)))
    for i in range(1, 11):
        placed = start + timedelta(minutes=5 * i)
        # Each of the others is priced from the one-minute candle after it was sent.
        failed = i in (1, 10)
        world.store.save_order(
            _order(
                f"o{i}",
                placed,
                filled=0 if failed else 1,
                outcome_at=placed + timedelta(minutes=1),
            )
        )

    rebuilt = _rebuilt(world)

    # In send order the last ten are o1..o10, two of them unfilled (o1 and o10). Ordered by
    # outcome they would be o2..o10 and o0, and reversed o9..o0: either way one unfilled order
    # falls out of the last ten and the stop would not hold.
    reasons = stop_reasons(world.store, world.config, world.clock, world.evidence)
    assert "fill failures: 2 of the last 10 orders did not fill" in reasons
    assert stop_reasons(rebuilt.store, rebuilt.config, rebuilt.clock, rebuilt.evidence) == reasons
    sent = [o.order_id for o in world.store.list_orders()]
    assert [o.order_id for o in rebuilt.store.list_orders()] == sent
    assert _answers(rebuilt) == _answers(world)


def _forecast(event_ticker: str, at: datetime) -> Forecast:
    return Forecast(
        forecast_id=f"forecast-{event_ticker}-{at.isoformat()}",
        episode_id="ep-earlier",
        event_ticker=event_ticker,
        as_of=at,
        outcome_probabilities={"hold": 1.0},
        market_probabilities={HOLD: 1.0},
        coverage=1.0,
        confidence=0.5,
        n_independent_origins=1,
    )


def test_the_rejections_and_last_forecast_an_episode_reads_are_in_the_rebuilt_world():
    world = _world()
    store = world.store
    store.record_rejection("ep-earlier", EVENT, "claim A", "unsupported")
    store.save_forecast(_forecast(EVENT, AS_OF - timedelta(days=1)))
    other = "KXOTHER-26DEC"  # an event the episode never asks about
    store.record_rejection("ep-earlier", other, "claim B", "unsupported")
    # Forecast at the same instant by an episode that started later: in this one's future.
    late = "KXLATE-26DEC"
    store.save_forecast(_forecast(late, AS_OF + timedelta(hours=1)))

    recording = Recording(world)
    seen = recording.world.store
    # What a price-move episode reads, then what its critic writes and the forecaster reads.
    assert seen.last_forecast_time(EVENT) == AS_OF - timedelta(days=1)
    assert seen.last_forecast_time(late) == AS_OF + timedelta(hours=1)
    seen.record_rejection("ep-now", EVENT, "claim C", "contradicted")
    assert len(seen.get_rejections(EVENT)) == 2
    file = recording.file(world.file.trigger, description="round trip")
    rebuilt = _world(file)

    # Only what the store held for the event before the episode touched it.
    assert file.rejections == [
        RecordedRejection(event_ticker=EVENT, claim="claim A", reason="unsupported")
    ]
    assert rebuilt.store.get_rejections(EVENT) == [
        RejectionBrief(claim="claim A", reason="unsupported")
    ]
    assert rebuilt.store.get_rejections(other) == []
    assert rebuilt.store.last_forecast_time(EVENT) == AS_OF - timedelta(days=1)
    assert rebuilt.store.last_forecast_time(late) is None
    assert _answers(rebuilt) == _answers(world)


def test_a_file_with_bare_fills_and_no_starting_state_fields_builds_as_before():
    file = world_file(fills=[prior_fill()])
    world = _world(file)
    [order] = world.store.list_orders()
    assert order.fills == [prior_fill()]
    assert [(p.market_ticker, p.contracts) for p in world.executor.positions()] == [(CUT, 5)]
    # One equity point at the trigger time, the starting account marked then: the bankroll
    # less the fill's cost and fee, plus the five NO contracts at the cut market's NO bid.
    assert world.store.equity_series() == [(AS_OF, EXPECTED_FIRST_EQUITY)]
    assert first_equity(file) == EXPECTED_FIRST_EQUITY
    assert world.store.list_settlements() == []


def test_a_file_giving_one_order_both_as_an_order_and_as_fills_is_refused():
    fill = prior_fill()
    order = _order(fill.order_id, fill.filled_at, filled=5, market=CUT, side=Side.NO)
    with pytest.raises(ConfigError, match=fill.order_id):
        _world(world_file(fills=[fill], orders=[order]))
