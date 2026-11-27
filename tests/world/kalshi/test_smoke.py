"""Opt-in: read one series from the live production API. Set PMFA_SMOKE=1 to run; never in CI."""

from __future__ import annotations

import os

import pytest

from pmfa.config import default_config
from pmfa.world.clocks import SystemClock
from pmfa.world.kalshi.production import production_exchange

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.environ.get("PMFA_SMOKE") != "1", reason="set PMFA_SMOKE=1 to call Kalshi"
    ),
]


def test_reads_one_series_from_production() -> None:
    exchange = production_exchange(default_config(), SystemClock())
    try:
        series = exchange.get_series("KXFEDDECISION")
        assert series.ticker == "KXFEDDECISION"
        assert series.fee_type
        events = exchange.list_events("KXFEDDECISION")
        assert events and all(e.series_ticker == "KXFEDDECISION" for e in events)
        assert all(e.markets for e in events)
    finally:
        exchange.client.close()
