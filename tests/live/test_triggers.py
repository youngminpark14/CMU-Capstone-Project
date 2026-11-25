"""Triggers: new documents, price moves from one-minute candles, daily re-checks; deterministic ids."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

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
            # C25 is 4c down at 15:06 from 15:01, but only 2c from its price at H0's
            # crossing (0.31 at 15:04): measured from the event's anchor, no second trigger.
            cut: mid_candles(cut, [(minute(1), "0.33"), (minute(4), "0.31"), (minute(6), "0.29")]),
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
    assert t.trigger_id == trig.price_move_trigger(FED, t.at, "").trigger_id
    # A pass in the next bucket finds the same one move, once.
    assert trig.price_move_triggers(exchange, [event], Config(), minute(65, base)) == [t]


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
    assert t.trigger_id == trig.price_move_trigger(FED, minute(-1), "").trigger_id


def _scan(points, times, base: datetime = BUCKET, *, anchored: bool = False) -> list[datetime]:
    """Run passes at ``base + times`` minutes; return every trigger's time, once each.

    ``anchored`` carries each event's last trigger from pass to pass, as the loop does.
    """
    event = fed_event()
    hold = f"{FED}-H0"
    exchange = FakeCandles({hold: mid_candles(hold, [(minute(n, base), p) for n, p in points])})
    seen: dict[str, datetime] = {}
    anchors: dict[str, datetime] = {}
    for n in times:
        for t in trig.price_move_triggers(
            exchange, [event], Config(), minute(n, base), anchors=anchors if anchored else None
        ):
            assert seen.setdefault(t.trigger_id, t.at) == t.at  # an id always means one time
            anchors[FED] = max(anchors.get(FED, t.at), t.at)
    return sorted(seen.values())


def test_a_second_step_inside_the_window_fires_again():
    # +3c at 10:25, +3c more at 10:45: the 0.50 of 10:00 is still in the window at
    # 10:25 and the 0.53 of 10:25 at 10:45, but each step is a move of its own.
    base = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    points = [(0, "0.50"), (25, "0.53"), (45, "0.56")]
    for anchored in (False, True):
        assert _scan(points, range(5, 90, 5), base, anchored=anchored) == [
            minute(25, base),
            minute(45, base),
        ]
    dense = [(n, "0.50" if n < 25 else "0.53" if n < 45 else "0.56") for n in range(70)]
    assert _scan(dense, range(5, 90), base) == [minute(25, base), minute(45, base)]


def test_a_reversal_fires_again():
    base = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    points = [(0, "0.50"), (20, "0.50"), (25, "0.53"), (35, "0.50")]
    assert _scan(points, range(5, 90, 5), base) == [minute(25, base), minute(35, base)]


def test_a_sustained_trend_fires_each_time_it_adds_up_to_the_threshold():
    # +1c every 10 minutes for two hours (12c): one trigger per 3c, every 30 minutes.
    base = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    trend = [(n, f"{Decimal('0.40') + Decimal('0.01') * (n // 10):.2f}") for n in range(121)]
    expected = [minute(n, base) for n in (30, 60, 90, 120)]
    assert _scan(trend, range(5, 130, 5), base) == expected
    assert _scan(trend, range(5, 130, 5), base, anchored=True) == expected


def test_an_uneven_trend_gives_the_same_triggers_from_pass_to_pass_with_anchors():
    # +1c every 7 minutes: with the last trigger carried over, a later pass continues
    # the same chain of crossings instead of starting a new one from its own first candle.
    base = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    trend = [(n, f"{Decimal('0.20') + Decimal('0.01') * (n // 7):.2f}") for n in range(300)]
    found = _scan(trend, range(1, 300, 3), base, anchored=True)
    steps = {(b - a) for a, b in pairwise(found)}
    assert steps == {timedelta(minutes=21)}  # every 3c, never an extra trigger in between


def test_a_thin_market_keeps_its_last_price_before_the_anchor_as_its_reference():
    # H0 crosses at 10:20 and moves the event's anchor there. C25 is priced only at 10:00
    # (0.20) and 10:35 (0.24): its 4c move came after the anchor, measured from 0.20.
    base = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    event = fed_event()
    hold, cut = f"{FED}-H0", f"{FED}-C25"
    candles = {
        hold: mid_candles(hold, [(minute(0, base), "0.50"), (minute(20, base), "0.53")]),
        cut: mid_candles(cut, [(minute(0, base), "0.20"), (minute(35, base), "0.24")]),
    }
    window, threshold = timedelta(minutes=30), Decimal("0.03")
    moves = trig.price_moves(candles, window=window, threshold=threshold)
    assert [(at, ticker) for at, ticker, _ in moves] == [
        (minute(20, base), hold),
        (minute(35, base), cut),
    ]
    # The same from an anchor carried over from an earlier pass, as the loop does.
    later = trig.price_moves(candles, window=window, threshold=threshold, anchor=minute(20, base))
    assert [(at, ticker) for at, ticker, _ in later] == [(minute(35, base), cut)]
    exchange = FakeCandles(candles)
    anchors: dict[str, datetime] = {}
    seen = {}
    for n in range(5, 70, 5):
        for t in trig.price_move_triggers(
            exchange, [event], Config(), minute(n, base), anchors=anchors
        ):
            seen[t.trigger_id] = t.at
            anchors[FED] = max(anchors.get(FED, t.at), t.at)
    assert sorted(seen.values()) == [minute(20, base), minute(35, base)]


def test_a_move_before_the_anchor_is_not_counted_again_after_it():
    # C25 moved 0.20 -> 0.24 at 10:15, before the event's last trigger at 10:20: that
    # move belongs before the anchor, and from it on C25's reference is 0.24, so a flat
    # 0.24 at 10:35 starts nothing.
    base = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    hold, cut = f"{FED}-H0", f"{FED}-C25"
    cut_points = [(minute(0, base), "0.20"), (minute(15, base), "0.24"), (minute(35, base), "0.24")]
    candles = {
        hold: mid_candles(hold, [(minute(0, base), "0.50"), (minute(20, base), "0.53")]),
        cut: mid_candles(cut, cut_points),
    }
    window, threshold = timedelta(minutes=30), Decimal("0.03")
    later = trig.price_moves(candles, window=window, threshold=threshold, anchor=minute(20, base))
    assert later == []


def test_markets_of_one_event_crossing_together_give_one_trigger():
    event = fed_event()
    hold, cut = f"{FED}-H0", f"{FED}-C25"
    exchange = FakeCandles(
        {
            hold: mid_candles(hold, [(minute(1), "0.60"), (minute(5), "0.64")]),
            cut: mid_candles(cut, [(minute(1), "0.34"), (minute(5), "0.30")]),
        }
    )
    [t] = trig.price_move_triggers(exchange, [event], Config(), NOW)
    assert t.at == minute(5) and cut in t.detail  # the first ticker names it


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
