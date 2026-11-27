"""The data adapter reports the events it lists but does not yield, so the snapshot can name them."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from pmfa.world.kalshi.data import KalshiData
from tests.world.kalshi.fakes import NOW, FakeKalshi, client, clock, config, fixture


def _fake(*, empty: str | None = None) -> FakeKalshi:
    _, sep = fixture("event_26SEP")
    jun = copy.deepcopy(sep)
    jun["event"]["event_ticker"] = "KXFEDDECISION-26JUN"
    _, listed = fixture("events_window")
    listed = copy.deepcopy(listed)
    if empty is not None:
        listed["events"].append({**listed["events"][0], "event_ticker": empty, "markets": []})
    fake = FakeKalshi()
    fake.add("GET", "/events", (200, listed))
    fake.add("GET", "/historical/markets", "hist_markets_series", series_ticker="KXFEDDECISION")
    fake.add("GET", "/events/KXFEDDECISION-26JUN", (200, jun))
    return fake


def test_events_outside_the_window_or_without_markets_are_reported_with_the_reason() -> None:
    dropped: dict[str, str] = {}
    adapter = KalshiData(client(_fake(empty="KXFEDDECISION-26NOV")), clock(NOW), config())
    events = list(
        adapter.iter_series_events(
            "KXFEDDECISION",
            closing_from=datetime(2026, 7, 1, tzinfo=UTC),
            closing_to=datetime(2026, 12, 31, tzinfo=UTC),
            dropped=dropped,
        )
    )
    assert [e.event_ticker for e in events] == [
        "KXFEDDECISION-26JUL",
        "KXFEDDECISION-26SEP",
        "KXFEDDECISION-26OCT",
    ]
    assert set(dropped) == {"KXFEDDECISION-26DEC", "KXFEDDECISION-26NOV"}
    assert "after the end of the window" in dropped["KXFEDDECISION-26DEC"]
    assert "no markets" in dropped["KXFEDDECISION-26NOV"]
