"""The REST client: retry and backoff, error mapping, pagination, throttling, signing."""

from __future__ import annotations

import httpx
import pytest

from pmfa.errors import ExchangeError, NotFound
from pmfa.world.kalshi.client import KalshiClient, KalshiExchangeError, KalshiNotFound
from pmfa.world.kalshi.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER
from tests.world.kalshi.fakes import DEMO, PROD, FakeKalshi, Sleeps, client, fixture


def test_429_then_success_backs_off_and_returns_the_body() -> None:
    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "too_many_requests", "message": "too many requests"}},
            headers={"x-ratelimit-reset": "2.785"},
        )

    fake = FakeKalshi().add("GET", "/series/KXFEDDECISION", rate_limited, "series_KXFEDDECISION")
    sleeps = Sleeps()
    body = client(fake, sleeps=sleeps).get("/series/KXFEDDECISION")
    assert body["series"]["ticker"] == "KXFEDDECISION"
    assert len(fake.requests) == 2
    assert 2.785 in sleeps.calls  # waited at least the advertised reset


def test_recorded_429_body_maps_to_exchange_error_after_retries() -> None:
    fake = FakeKalshi().add("GET", "/events", "rate_limited_429")
    c = client(fake)
    with pytest.raises(KalshiExchangeError) as info:
        c.get("/events")
    assert info.value.status == 429
    assert info.value.code == "too_many_requests"
    assert len(fake.requests) == c.max_retries + 1


def test_404_is_not_found_with_the_exchange_code() -> None:
    fake = FakeKalshi().add("GET", "/markets/KXFEDDECISION-26SEP-H0", "market_26SEP_H0_live_404")
    with pytest.raises(NotFound) as info:
        client(fake).get("/markets/KXFEDDECISION-26SEP-H0")
    assert isinstance(info.value, KalshiNotFound)
    assert info.value.code == "not_found"
    assert len(fake.requests) == 1


def test_401_is_an_exchange_error_and_is_not_retried() -> None:
    fake = FakeKalshi().add("GET", "/portfolio/balance", "demo_unauthenticated_401")
    with pytest.raises(ExchangeError) as info:
        client(fake, DEMO).get("/portfolio/balance")
    assert info.value.status == 401
    assert info.value.code == "token_authentication_failure"
    assert len(fake.requests) == 1


def test_5xx_is_retried_for_get_but_never_for_post() -> None:
    fake = FakeKalshi()
    fake.add(
        "GET", "/historical/cutoff", (503, {"error": {"code": "service_unavailable"}}), "cutoff"
    )
    fake.add(
        "POST", "/portfolio/events/orders", (500, {"error": {"code": "internal_server_error"}})
    )
    c = client(fake)
    assert "market_settled_ts" in c.get("/historical/cutoff")
    with pytest.raises(KalshiExchangeError) as info:
        c.post("/portfolio/events/orders", json={"ticker": "X"})
    assert info.value.outcome_unknown
    assert len(fake.sent("POST", "/portfolio/events/orders")) == 1


def test_top_level_error_shape_and_validation_msg_are_read() -> None:
    fake = FakeKalshi()
    fake.add("POST", "/a", (400, {"code": "bad_request", "message": "bad request", "details": "x"}))
    fake.add("GET", "/b", (400, {"msg": "Parameter validation failed for GetMarkets"}))
    c = client(fake)
    with pytest.raises(KalshiExchangeError) as first:
        c.post("/a")
    assert (first.value.code, first.value.details) == ("bad_request", "x")
    with pytest.raises(KalshiExchangeError) as second:
        c.get("/b")
    assert "Parameter validation failed" in second.value.message


def test_transport_errors_are_retried_for_get_then_reported() -> None:
    attempts = []

    def broken(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("boom", request=request)

    fake = FakeKalshi().add("GET", "/x", broken)
    c = client(fake)
    with pytest.raises(KalshiExchangeError) as info:
        c.get("/x")
    assert info.value.status is None and info.value.outcome_unknown
    assert len(attempts) == c.max_retries + 1


def test_pagination_follows_the_cursor_to_the_end() -> None:
    fake = FakeKalshi()
    p1_status, p1 = fixture("events_open_p1")
    fake.add("GET", "/events", "events_open_p2", cursor=p1["cursor"])
    fake.add("GET", "/events", (p1_status, p1))
    items = list(client(fake).paginate("/events", {"series_ticker": "KXFEDDECISION"}, "events"))
    assert [e["event_ticker"] for e in items] == [
        "KXFEDDECISION-26DEC",
        "KXFEDDECISION-27JAN",
        "KXFEDDECISION-27MAR",
        "KXFEDDECISION-27APR",
    ]
    assert len(fake.requests) == 2
    assert all(r.url.params["series_ticker"] == "KXFEDDECISION" for r in fake.requests)


def test_a_repeated_cursor_stops_instead_of_looping() -> None:
    fake = FakeKalshi().add(
        "GET", "/markets", (200, {"markets": [{"ticker": "A"}], "cursor": "c1"})
    )
    with pytest.raises(ExchangeError):
        list(client(fake).paginate("/markets", None, "markets"))
    assert len(fake.requests) == 2


def test_rate_limiter_spaces_requests() -> None:
    fake = FakeKalshi().add("GET", "/historical/cutoff", "cutoff")
    sleeps = Sleeps()
    c = KalshiClient(
        PROD,
        max_requests_per_second=2.0,
        transport=fake.transport(),
        sleep=sleeps.sleep,
        monotonic=sleeps.monotonic,
    )
    for _ in range(3):
        c.get("/historical/cutoff")
    assert sleeps.calls == [0.5, 0.5]


def test_signed_requests_sign_the_full_path_without_query() -> None:
    class RecordingSigner:
        def __init__(self) -> None:
            self.signed: list[tuple[str, str]] = []

        def headers(self, method: str, path: str) -> dict[str, str]:
            self.signed.append((method, path))
            return {TIMESTAMP_HEADER: "1", SIGNATURE_HEADER: "sig"}

    signer = RecordingSigner()
    fake = FakeKalshi().add("GET", "/portfolio/balance", "constructed_balance")
    client(fake, DEMO, signer=signer).get("/portfolio/balance", {"subaccount": 1})
    assert signer.signed == [("GET", "/trade-api/v2/portfolio/balance")]
    assert fake.requests[0].headers[SIGNATURE_HEADER] == "sig"
    assert fake.requests[0].url.params["subaccount"] == "1"


def test_none_params_are_dropped_and_booleans_spelled() -> None:
    fake = FakeKalshi().add("GET", "/events", (200, {"events": [], "cursor": ""}))
    client(fake).get("/events", {"a": None, "with_nested_markets": True})
    assert dict(fake.requests[0].url.params) == {"with_nested_markets": "true"}


def test_empty_204_body_is_an_empty_dict() -> None:
    fake = FakeKalshi().add("DELETE", "/portfolio/events/orders", (204, None))
    assert client(fake, DEMO).delete("/portfolio/events/orders") == {}


def test_only_https_base_urls() -> None:
    with pytest.raises(ValueError):
        KalshiClient("http://external-api.kalshi.com/trade-api/v2")
