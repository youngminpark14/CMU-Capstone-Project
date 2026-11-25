"""The production exchange adapter: public market data only, read-only by construction.

Three things make it impossible for this adapter to trade, and a test checks
each one:

1. :class:`ProductionExchange` has no order, cancel, balance, position or
   other portfolio methods. It is the ``Exchange`` port and nothing more.
2. Its client is built here without a signer, and the class refuses a client
   that carries one, so no request to production is ever authenticated.
3. :func:`production_exchange` never reads the credential environment
   variables: this module does not import the signing module at all.

Shadow mode reads production prices through it; orders in shadow mode go to
the paper executor.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from pmfa.config import Config
from pmfa.errors import ConfigError
from pmfa.ports import Clock
from pmfa.world.kalshi.client import KalshiClient
from pmfa.world.kalshi.data import KalshiData


class ProductionExchange(KalshiData):
    """Read-only market data from the production host. It cannot sign, so it cannot trade."""

    def __init__(self, client: KalshiClient, clock: Clock, config: Config) -> None:
        if client.signer is not None:
            raise ConfigError("the production exchange adapter must not carry a signer")
        super().__init__(client, clock, config)


def production_exchange(
    config: Config,
    clock: Clock,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProductionExchange:
    """An unsigned, throttled client on ``config.kalshi.production_base_url``, wrapped read-only."""
    client = KalshiClient(
        config.kalshi.production_base_url,
        signer=None,
        max_requests_per_second=config.kalshi.max_requests_per_second,
        timeout=config.kalshi.request_timeout_seconds,
        transport=transport,
        user_agent=config.sources.user_agent,
        sleep=sleep,
        monotonic=monotonic,
    )
    return ProductionExchange(client, clock, config)


__all__ = ["ProductionExchange", "production_exchange"]
