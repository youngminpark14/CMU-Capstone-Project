"""Opt-in: one real model call through the gateway. Never runs in CI.

Runs only with ``PMFA_SMOKE=1`` and the Claude API key (the variable named in
``config.models.api_key_env``, ``ANTHROPIC_API_KEY`` by default) in the environment.
It sends the small Monitor match input from ``fixtures.py`` once, on the cheapest
configured slot, and checks that a validated record comes back with its cost metered.
"""

from __future__ import annotations

import os

import pytest

from pmfa.config import default_config
from pmfa.models import Gateway
from pmfa.records.roles import MatchDecision, RoleTask

from .fixtures import INPUTS

CONFIG = default_config()

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.environ.get("PMFA_SMOKE") != "1", reason="set PMFA_SMOKE=1 to call the Claude API"
    ),
    pytest.mark.skipif(
        not os.environ.get(CONFIG.models.api_key_env),
        reason=f"needs {CONFIG.models.api_key_env}",
    ),
]


def cheapest_slot():
    """The configured slot whose model has the lowest output price (then input price)."""
    catalog = CONFIG.models.catalog

    def price(item):
        info = catalog[item[1].model]
        return (info.output_per_mtok, info.input_per_mtok)

    return min(CONFIG.models.slots.items(), key=price)[0]


def test_one_gateway_call_returns_a_validated_record() -> None:
    gateway = Gateway(CONFIG)
    result = gateway.call(
        RoleTask.MATCH, INPUTS[RoleTask.MATCH], episode_id="smoke", slot=cheapest_slot()
    )
    assert isinstance(result.output, MatchDecision)
    assert result.meta.source == "live"
    usage = gateway.usage("smoke")
    assert 1 <= usage.calls <= 2  # one call, or one call and its repair retry
    assert usage.cost_usd > 0
