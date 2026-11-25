"""Opt-in: read the balance and one market from the Kalshi demo exchange. Never runs in CI.

Runs only with ``PMFA_SMOKE=1`` and the demo credentials (``KALSHI_DEMO_KEY_ID`` plus
``KALSHI_DEMO_PRIVATE_KEY`` or ``KALSHI_DEMO_PRIVATE_KEY_PATH``) in the environment.
It is read-only: it signs one balance request and reads one followed-series market,
and it places no order.
"""

from __future__ import annotations

import os

import pytest

from pmfa.config import default_config
from pmfa.world.clocks import SystemClock
from pmfa.world.kalshi.demo import demo_executor

CONFIG = default_config()
_KALSHI = CONFIG.kalshi
_HAS_DEMO_KEY = bool(os.environ.get(_KALSHI.demo_key_id_env)) and bool(
    os.environ.get(_KALSHI.demo_private_key_env)
    or os.environ.get(_KALSHI.demo_private_key_path_env)
)

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.environ.get("PMFA_SMOKE") != "1", reason="set PMFA_SMOKE=1 to call the demo exchange"
    ),
    pytest.mark.skipif(
        not _HAS_DEMO_KEY,
        reason=f"needs {_KALSHI.demo_key_id_env} and {_KALSHI.demo_private_key_env} "
        f"or {_KALSHI.demo_private_key_path_env}",
    ),
]


def test_reads_the_balance_and_one_market_from_the_demo_exchange() -> None:
    executor = demo_executor(CONFIG, SystemClock())
    try:
        balance = executor.balance()
        assert balance.cash >= 0

        market = None
        for series in CONFIG.follow.enabled_series():
            events = executor.data.list_events(series.ticker)
            if events and events[0].markets:
                market = executor.data.get_market(events[0].markets[0].ticker)
                break
        assert market is not None, "no followed series has an open event on the demo exchange"
        assert market.ticker
    finally:
        executor.client.close()
        executor.data.client.close()
