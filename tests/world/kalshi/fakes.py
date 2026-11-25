"""A recorded Kalshi for tests: fixture files served through ``httpx.MockTransport``.

Routes match a method and a path (relative to ``/trade-api/v2``) and, when
given, a subset of query parameters. Each route answers with a list of
responses in turn (the last one repeats), so a test can script a 429 and then
a success. Every request is recorded for assertions on what was sent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from pmfa.config import Config, default_config
from pmfa.world.clocks import SimClock
from pmfa.world.kalshi.client import KalshiClient

FIXTURES = Path(__file__).parent / "fixtures"
PREFIX = "/trade-api/v2"
PROD = "https://external-api.kalshi.com/trade-api/v2"
DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
NOW = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)


def fixture(name: str) -> tuple[int, Any]:
    """``(status, body)`` of a fixture file."""
    doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return doc["status"], doc["body"]


Reply = tuple[int, Any] | Callable[[httpx.Request], httpx.Response]


@dataclass
class Route:
    method: str
    path: str
    params: dict[str, str]
    replies: list[Reply]
    calls: int = 0

    def matches(self, request: httpx.Request) -> bool:
        if request.method != self.method or request.url.path != PREFIX + self.path:
            return False
        query = dict(request.url.params)
        return all(query.get(k) == v for k, v in self.params.items())

    def reply(self, request: httpx.Request) -> httpx.Response:
        answer = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if callable(answer):
            return answer(request)
        status, body = answer
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)


@dataclass
class FakeKalshi:
    """Routes plus a log of the requests that reached them."""

    routes: list[Route] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, method: str, path: str, *replies: Reply | str, **params: Any) -> FakeKalshi:
        """Answer ``method path`` (with these query params) with fixtures or ``(status, body)``s."""
        resolved = [fixture(r) if isinstance(r, str) else r for r in replies]
        query = {k: _wire(v) for k, v in params.items()}
        # More specific routes (more parameters) are tried first; among equals, the latest added.
        self.routes.insert(0, Route(method.upper(), path, query, resolved))
        self.routes.sort(key=lambda r: -len(r.params))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for route in self.routes:
            if route.matches(request):
                return route.reply(request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == PREFIX + path]


def _wire(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Sleeps:
    """A ``sleep`` that records instead of waiting, and a matching monotonic clock."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.now = 1000.0

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def client(
    fake: FakeKalshi, base_url: str = PROD, *, signer: Any = None, sleeps: Sleeps | None = None
) -> KalshiClient:
    sleeps = sleeps or Sleeps()
    return KalshiClient(
        base_url,
        signer=signer,
        max_requests_per_second=1000.0,
        timeout=5.0,
        transport=fake.transport(),
        sleep=sleeps.sleep,
        monotonic=sleeps.monotonic,
    )


def config() -> Config:
    return default_config()


def clock(at: datetime = NOW) -> SimClock:
    return SimClock(at)
