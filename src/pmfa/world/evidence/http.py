"""A polite fetcher for the official source sites.

Federal Reserve and BLS pages are fetched with these rules, all of them in code
so no caller can forget one:

- **Generic User-Agent.** ``config.sources.user_agent`` is sent as is. It names
  the client and nothing else: never a person, an email address, or a phone
  number.
- **Spacing.** At least ``config.sources.min_seconds_between_requests`` passes
  between two requests to one host. The wait is measured on an injectable clock
  and spent in an injectable ``sleep``, so tests run without waiting.
- **Fetch once.** Every answer is stored on disk, keyed by URL, and later calls
  read the stored copy. A refusal (``401``, ``403``, ``404``) is stored too, so
  a refused URL is not asked again. Pass ``refresh=True`` to fetch again, which
  the live poller needs for the listings that change; it is still spaced.
- **A clear error when a site refuses.** ``401``, ``403``, ``429`` and ``451``
  raise :class:`FetchRefused`; any other failure raises :class:`FetchError`.
  Nothing retries, so a refusal is never answered with more traffic.

Tests pass an ``httpx.MockTransport`` as ``transport``; nothing here opens a
connection otherwise unless a caller builds the fetcher for a real run.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx

from pmfa.errors import PmfaError

if TYPE_CHECKING:
    from pmfa.config import Config

REFUSAL_STATUSES = frozenset({401, 403, 429, 451})
"""Statuses that mean "this site does not want this client", not "this page is missing"."""

CACHED_FAILURE_STATUSES = frozenset({401, 403, 404, 410, 451})
"""Failures worth remembering: asking again gets the same answer. 429 and 5xx are transient."""


class FetchError(PmfaError):
    """A page could not be fetched: a missing page, a server error, or a dropped connection."""

    def __init__(self, url: str, status: int | None, message: str) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class FetchRefused(FetchError):
    """The site refused this client (401, 403, 429 or 451).

    Callers that have a manual path (BLS) catch this and fall back to it.
    """


@dataclass(frozen=True)
class Fetched:
    """One successful answer."""

    url: str
    status: int
    content: bytes
    from_cache: bool

    @property
    def text(self) -> str:
        """The body as text. A UTF-8 byte-order mark (federalreserve.gov's JSON has one) is dropped."""
        return self.content.decode("utf-8-sig", errors="replace")


class Fetcher(Protocol):
    """What the source adapters need from a fetcher."""

    def get(self, url: str, *, refresh: bool = False) -> Fetched: ...


class PoliteFetcher:
    """Fetches URLs one at a time, spaced per host, cached on disk, with a generic User-Agent."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        user_agent: str,
        min_seconds_between_requests: float,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._user_agent = user_agent
        self._min_gap = min_seconds_between_requests
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            follow_redirects=True,
            timeout=timeout_seconds,
        )
        self.requests_made = 0
        """Network requests sent by this fetcher (cache hits do not count)."""

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        cache_dir: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> PoliteFetcher:
        """A fetcher using the user agent and spacing from ``config.sources``."""
        return cls(
            cache_dir if cache_dir is not None else config.paths.cache_dir() / "evidence",
            user_agent=config.sources.user_agent,
            min_seconds_between_requests=config.sources.min_seconds_between_requests,
            transport=transport,
            sleep=sleep,
            monotonic=monotonic,
        )

    def close(self) -> None:
        self._client.close()

    # -- cache ---------------------------------------------------------------------------

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self._cache_dir / f"{key}.body", self._cache_dir / f"{key}.json"

    def _read_cache(self, url: str) -> Fetched | None:
        body_path, meta_path = self._paths(url)
        if not (body_path.exists() and meta_path.exists()):
            return None
        meta = json.loads(meta_path.read_text())
        status = int(meta["status"])
        if status in CACHED_FAILURE_STATUSES:
            raise _failure(url, status, remembered=True)
        return Fetched(url=url, status=status, content=body_path.read_bytes(), from_cache=True)

    def _write_cache(self, url: str, status: int, content: bytes) -> None:
        body_path, meta_path = self._paths(url)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(content)
        meta_path.write_text(json.dumps({"url": url, "status": status}))

    # -- network -------------------------------------------------------------------------

    def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = last + self._min_gap - self._monotonic()
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._monotonic()

    def get(self, url: str, *, refresh: bool = False) -> Fetched:
        """The page at ``url``: from disk if fetched before, else from the site (spaced)."""
        if not refresh:
            cached = self._read_cache(url)
            if cached is not None:
                return cached
        host = urlsplit(url).hostname or ""
        self._wait_turn(host)
        self.requests_made += 1
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            # The exception text can hold the URL only, never request headers.
            raise FetchError(url, None, f"could not reach {url}: {type(exc).__name__}") from exc
        status = response.status_code
        # The spacing clock restarts when the answer arrives, so a slow answer is not a burst.
        self._last_request[host] = self._monotonic()
        if status in CACHED_FAILURE_STATUSES:
            self._write_cache(url, status, b"")
        if status >= 400:
            raise _failure(url, status, remembered=False)
        self._write_cache(url, status, response.content)
        return Fetched(url=url, status=status, content=response.content, from_cache=False)


def _failure(url: str, status: int, *, remembered: bool) -> FetchError:
    again = " (answer remembered from an earlier request; delete the cache entry to ask again)"
    note = again if remembered else ""
    if status in REFUSAL_STATUSES:
        return FetchRefused(
            url, status, f"{urlsplit(url).hostname} refused the request: HTTP {status}{note}"
        )
    return FetchError(url, status, f"{url} answered HTTP {status}{note}")
