"""The polite fetcher: generic User-Agent, spacing, fetch once, clear refusal errors."""

from __future__ import annotations

import httpx
import pytest

from pmfa.config import Config
from pmfa.world.evidence.http import FetchError, FetchRefused, PoliteFetcher


class Clock:
    """A fake clock whose sleep moves it forward, so spacing is measured without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_fetcher(tmp_path, handler, clock: Clock, gap: float = 2.0) -> PoliteFetcher:
    return PoliteFetcher(
        tmp_path / "cache",
        user_agent="pmfa-research-client/0.1",
        min_seconds_between_requests=gap,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def test_user_agent_is_generic_and_sent(tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, text="ok")

    fetcher = PoliteFetcher.from_config(
        Config(), cache_dir=tmp_path, transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    fetcher.get("https://example.gov/a")
    assert seen == [Config().sources.user_agent]
    assert "@" not in seen[0]
    assert not any(ch.isdigit() for ch in seen[0].replace("0.1", ""))


def test_requests_to_one_host_are_spaced(tmp_path):
    clock = Clock()
    fetcher = make_fetcher(tmp_path, lambda r: httpx.Response(200, text=r.url.path), clock)
    fetcher.get("https://example.gov/a")
    fetcher.get("https://example.gov/b")
    fetcher.get("https://other.gov/c")  # another host: no wait
    clock.now += 5  # long enough since the last request: no wait
    fetcher.get("https://example.gov/d")
    assert clock.slept == [pytest.approx(2.0)]


def test_every_url_is_fetched_once(tmp_path):
    clock = Clock()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"\xef\xbb\xbf[1]")

    first = make_fetcher(tmp_path, handler, clock)
    page = first.get("https://example.gov/x.json")
    assert page.text == "[1]"  # the byte-order mark is dropped
    assert not page.from_cache
    # A second fetcher over the same cache directory also reads from disk.
    second = make_fetcher(tmp_path, handler, clock)
    again = second.get("https://example.gov/x.json")
    assert again.from_cache and again.text == "[1]"
    assert calls == ["https://example.gov/x.json"]
    # refresh asks the site again (still spaced).
    second.get("https://example.gov/x.json", refresh=True)
    assert len(calls) == 2


def test_refusal_raises_clear_error_and_is_remembered(tmp_path):
    clock = Clock()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(403, text="Access Denied")

    fetcher = make_fetcher(tmp_path, handler, clock)
    with pytest.raises(FetchRefused) as first:
        fetcher.get("https://www.bls.gov/robots.txt")
    assert first.value.status == 403
    assert "bls.gov" in str(first.value)
    with pytest.raises(FetchRefused):
        fetcher.get("https://www.bls.gov/robots.txt")
    assert len(calls) == 1  # the refusal was not asked again


def test_other_failures_are_fetch_errors_not_refusals(tmp_path):
    clock = Clock()
    statuses = iter([404, 503])
    fetcher = make_fetcher(tmp_path, lambda r: httpx.Response(next(statuses)), clock)
    with pytest.raises(FetchError) as missing:
        fetcher.get("https://example.gov/missing")
    assert not isinstance(missing.value, FetchRefused) and missing.value.status == 404
    with pytest.raises(FetchError) as down:
        fetcher.get("https://example.gov/down")
    assert not isinstance(down.value, FetchRefused)


def test_a_transient_failure_is_not_remembered(tmp_path):
    clock = Clock()
    answers = iter([httpx.Response(503), httpx.Response(200, text="back")])
    fetcher = make_fetcher(tmp_path, lambda r: next(answers), clock)
    with pytest.raises(FetchError):
        fetcher.get("https://example.gov/flaky")
    assert fetcher.get("https://example.gov/flaky").text == "back"


def test_network_error_is_a_fetch_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    fetcher = make_fetcher(tmp_path, handler, Clock())
    with pytest.raises(FetchError) as err:
        fetcher.get("https://example.gov/a")
    assert err.value.status is None
