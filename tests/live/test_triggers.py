"""Triggers: new documents, price moves from one-minute candles, daily re-checks; deterministic ids."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pmfa.config import Config, load_config
from pmfa.live import triggers as trig
from pmfa.records.evidence import TriggerKind
from pmfa.records.market import MarketStatus, Position, Side
from tests.episodes.support import FED, MINUTES, NEWS, SPEECH, fed_event

from .support import mid_candles

NOW = datetime(2026, 11, 25, 15, 7, tzinfo=UTC)
BUCKET = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # the 30-minute bucket holding NOW


class FakeCandles:
    """An exchange that serves only candles, per market."""

    def __init__(self, candles: dict[str, list]) -> None:
        self.candles = candles
        self.asked: list[tuple[str, datetime, datetime]] = []

    def get_candles(self, ticker, *, start, end, interval_minutes):  # type: ignore[no-untyped-def]
        assert interval_minutes == 1
        self.asked.append((ticker, start, end))
        return [c for c in self.candles.get(ticker, []) if start < c.end_ts <= end]


def minute(n: int, base: datetime = BUCKET) -> datetime:
    return base + timedelta(minutes=n)


# --------------------------------------------------------------------------- documents


def test_document_triggers_come_from_followed_sources_once_each_with_stable_ids():
    config = Config()
    found = trig.document_triggers([SPEECH, NEWS, MINUTES, MINUTES], config)
    assert [t.doc_id for t in found] == [
        SPEECH.doc_id,
        MINUTES.doc_id,
    ]  # news.example is not followed
    assert all(t.kind is TriggerKind.NEW_DOCUMENT for t in found)
    assert found[1].at == MINUTES.published_at
    again = trig.document_triggers([MINUTES], config)
    assert again[0] == found[1]


# --------------------------------------------------------------------------- price moves


def test_a_three_cent_move_within_the_window_starts_one_trigger_for_the_event():
    event = fed_event()
    hold, cut = f"{FED}-H0", f"{FED}-C25"
    exchange = FakeCandles(
        {
            # the price at the bucket's start comes from the last candle before it
            hold: mid_candles(
                hold, [(minute(-10), "0.62"), (minute(2), "0.64"), (minute(5), "0.65")]
            ),
            cut: mid_candles(cut, [(minute(1), "0.33"), (minute(6), "0.29")]),
        }
    )
    found = trig.price_move_triggers(exchange, [event], Config(), NOW)
    assert len(found) == 1  # one per event per window, not per market
    [t] = found
    assert t.kind is TriggerKind.PRICE_MOVE and t.event_ticker == FED
    assert t.at == minute(5)  # the earliest crossing among the event's markets
    assert hold in t.detail
    # Scanning again (a later pass in the same window) gives the same trigger.
    later = trig.price_move_triggers(exchange, [event], Config(), NOW + timedelta(minutes=3))
    assert later == found


def test_a_smaller_move_or_a_move_spread_over_two_windows_starts_nothing():
    event = fed_event()
    hold = f"{FED}-H0"
    small = FakeCandles({hold: mid_candles(hold, [(minute(1), "0.62"), (minute(20), "0.64")])})
    assert trig.price_move_triggers(small, [event], Config(), NOW) == []
    # 2c, then 2c more 32 minutes later: no 30-minute span saw 3c.
    slow = FakeCandles(
        {
            hold: mid_candles(
                hold, [(minute(-40), "0.60"), (minute(-12), "0.62"), (minute(20), "0.64")]
            )
        }
    )
    assert trig.price_move_triggers(slow, [event], Config(), minute(25)) == []


def test_a_move_within_thirty_minutes_that_spans_a_bucket_edge_starts_a_trigger():
    # 10:20 -> 10:40 is a 4c move in 20 minutes, across the 10:30 edge of the fixed buckets.
    event = fed_event()
    hold = f"{FED}-H0"
    base = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    points = [(base, "0.50"), (minute(20, base), "0.50")]
    points += [(minute(30, base), "0.52"), (minute(40, base), "0.54")]
    exchange = FakeCandles({hold: mid_candles(hold, points)})
    [t] = trig.price_move_triggers(exchange, [event], Config(), minute(41, base))
    assert t.at == minute(40, base)
    assert t.trigger_id == trig.price_move_trigger(FED, minute(30, base), t.at, "").trigger_id


def test_one_step_fires_once_even_while_it_stays_inside_the_window():
    event = fed_event()
    hold = f"{FED}-H0"
    points = [(minute(-40), "0.50"), (minute(20), "0.54"), (minute(32), "0.54")]
    points += [(minute(45), "0.54")]
    exchange = FakeCandles({hold: mid_candles(hold, points)})
    [first] = trig.price_move_triggers(exchange, [event], Config(), minute(25))
    assert first.at == minute(20)
    # A pass in the next bucket still sees 0.50 inside the window of 15:32, but the move is the same.
    assert trig.price_move_triggers(exchange, [event], Config(), minute(46)) == [first]


def test_a_move_late_in_the_previous_window_is_still_found_in_the_next_pass():
    event = fed_event()
    hold = f"{FED}-H0"
    exchange = FakeCandles({hold: mid_candles(hold, [(minute(-20), "0.62"), (minute(-1), "0.66")])})
    [t] = trig.price_move_triggers(exchange, [event], Config(), minute(4))
    assert t.at == minute(-1)
    assert t.trigger_id == trig.price_move_trigger(FED, minute(-30), minute(-1), "").trigger_id


def test_only_open_markets_of_followed_series_are_read():
    event = fed_event()
    closed = event.model_copy(
        update={
            "markets": [m.model_copy(update={"status": MarketStatus.CLOSED}) for m in event.markets]
        }
    )
    unfollowed = event.model_copy(update={"series_ticker": "KXOTHER", "event_ticker": "KXOTHER-1"})
    exchange = FakeCandles({})
    assert trig.price_move_triggers(exchange, [closed, unfollowed], Config(), NOW) == []
    assert exchange.asked == []


def test_the_window_and_threshold_come_from_the_configuration():
    config = load_config(
        overrides={"triggers": {"price_move_cents": 2, "price_move_window_minutes": 60}}
    )
    event = fed_event()
    hold = f"{FED}-H0"
    exchange = FakeCandles({hold: mid_candles(hold, [(minute(1), "0.62"), (minute(20), "0.64")])})
    [t] = trig.price_move_triggers(exchange, [event], config, minute(25))
    assert t.at == minute(20)
    assert trig.price_move_triggers(exchange, [event], Config(), minute(25)) == []


# --------------------------------------------------------------------------- schedule


class FakeHoldings:
    simulated = True

    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions

    def positions(self, event_ticker=None):  # type: ignore[no-untyped-def]
        return self._positions


def test_a_daily_recheck_of_each_held_followed_event_when_configured():
    held = Position(
        market_ticker=f"{FED}-C25", event_ticker=FED, side=Side.NO, contracts=4, avg_price="0.68"
    )
    other = Position(
        market_ticker="KXOTHER-1-A",
        event_ticker="KXOTHER-1",
        side=Side.YES,
        contracts=1,
        avg_price=Decimal("0.5"),
    )
    executor = FakeHoldings([held, other])
    assert trig.schedule_triggers(executor, Config(), NOW) == []  # off by default

    config = load_config(overrides={"triggers": {"daily_recheck_open_positions": True}})
    [t] = trig.schedule_triggers(executor, config, NOW)
    assert t.kind is TriggerKind.SCHEDULE and t.event_ticker == FED
    assert t.at == datetime(2026, 11, 25, 5, 0, tzinfo=UTC)  # midnight in New York
    later = trig.schedule_triggers(executor, config, NOW + timedelta(hours=6))
    assert later == [t]  # the same tick all day
    tomorrow = trig.schedule_triggers(executor, config, NOW + timedelta(days=1))
    assert tomorrow[0].trigger_id != t.trigger_id
    assert trig.schedule_triggers(executor, config, NOW, open_events=[]) == []


def test_combine_orders_by_time_and_drops_repeats():
    a = trig.document_trigger(MINUTES)
    b = trig.document_trigger(SPEECH)
    assert trig.combine([a, b], [a]) == [b, a]
