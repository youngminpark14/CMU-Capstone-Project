"""A thin REST client for the dozen Kalshi endpoints this project uses.

Kalshi's own SDKs can lag the API, so the project talks to it directly. The
client does four things and nothing else:

- **Throttling.** Requests are spaced at least ``1 / max_requests_per_second``
  apart, so a run stays inside the basic rate tier.
- **Bounded retry.** A ``429`` is retried for every method: the exchange did
  not process the request. A ``5xx`` or a dropped connection is retried only
  for ``GET`` and ``DELETE``, which are safe to repeat. A ``POST`` that fails
  that way is *not* retried, because Kalshi documents that the order may still
  have been placed; the caller must read the outcome back instead.
- **Signing.** When a signer is given, every request carries the three
  ``KALSHI-ACCESS-*`` headers, computed over the exact path that is sent.
- **Error mapping.** A ``404`` raises :class:`KalshiNotFound` (a
  :class:`pmfa.errors.NotFound`); any other failure raises
  :class:`KalshiExchangeError` (a :class:`pmfa.errors.ExchangeError`). Both carry
  the exchange's error ``code``, ``message`` and ``details``.

The ``transport`` argument lets tests inject ``httpx.MockTransport``; ``sleep``
and ``monotonic`` let them run the limiter and the backoff without waiting.
Request headers are never logged, so neither are signatures or key ids.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from pmfa.errors import ExchangeError, NotFound

if TYPE_CHECKING:
    from pmfa.world.kalshi.signing import KalshiSigner

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "pmfa-research-client/0.1"
"""Generic on purpose: no personal or contact details go to the exchange."""

RETRY_STATUSES_ANY_METHOD = frozenset({429})
RETRY_STATUSES_IDEMPOTENT = frozenset({500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "DELETE"})


class _KalshiErrorFields:
    """The fields Kalshi puts in an error body, shared by both error classes."""

    status: int | None
    code: str
    message: str
    details: str
    method: str
    path: str

    def _init_fields(
        self,
        *,
        status: int | None,
        code: str,
        message: str,
        details: str,
        method: str,
        path: str,
    ) -> str:
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.method = method
        self.path = path
        text = f"{method} {path} -> {status if status is not None else 'no response'}"
        if code:
            text += f" {code}"
        if message and message != code:
            text += f": {message}"
        if details:
            text += f" ({details})"
        return text


class KalshiExchangeError(_KalshiErrorFields, ExchangeError):
    """The exchange refused or failed a request (any status but 404, or no response).

    ``status`` is ``None`` when no response arrived (timeout or dropped
    connection). For a ``POST`` that means the outcome is unknown.
    """

    def __init__(
        self,
        *,
        status: int | None,
        code: str = "",
        message: str = "",
        details: str = "",
        method: str = "",
        path: str = "",
    ) -> None:
        super().__init__(
            self._init_fields(
                status=status, code=code, message=message, details=details, method=method, path=path
            )
        )

    @property
    def outcome_unknown(self) -> bool:
        """True when the exchange may have processed the request anyway (408, 5xx, no response)."""
        return self.status is None or self.status == 408 or self.status >= 500


class KalshiNotFound(_KalshiErrorFields, NotFound):
    """The exchange answered 404: no such series, event, market, order, or group."""

    def __init__(
        self,
        *,
        code: str = "",
        message: str = "",
        details: str = "",
        method: str = "",
        path: str = "",
    ) -> None:
        super().__init__(
            self._init_fields(
                status=404, code=code, message=message, details=details, method=method, path=path
            )
        )


def error_fields(response: httpx.Response) -> tuple[str, str, str]:
    """``(code, message, details)`` from an error body, whichever shape it has.

    Kalshi normally nests them under ``error``; an unparsable request body gets
    them at the top level; a parameter-validation failure sends ``msg``.
    """
    try:
        body = response.json()
    except ValueError:
        return "", response.text.strip()[:300], ""
    if not isinstance(body, dict):
        return "", str(body)[:300], ""
    inner = body.get("error")
    fields = inner if isinstance(inner, dict) else body
    code = str(fields.get("code") or "")
    message = str(fields.get("message") or fields.get("msg") or "")
    details = str(fields.get("details") or "")
    return code, message, details


class RateLimiter:
    """Spaces request starts at least ``1 / per_second`` seconds apart."""

    def __init__(
        self,
        per_second: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_second <= 0:
            raise ValueError("max_requests_per_second must be positive")
        self.interval = 1.0 / per_second
        self._sleep = sleep
        self._monotonic = monotonic
        self._next_at: float | None = None

    def wait(self) -> None:
        now = self._monotonic()
        if self._next_at is not None and now < self._next_at:
            self._sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + self.interval


class KalshiClient:
    """JSON over HTTPS to one Kalshi host, throttled, retried, optionally signed.

    ``base_url`` includes the API prefix, e.g.
    ``https://external-api.kalshi.com/trade-api/v2``; request paths are relative
    to it (``/markets/{ticker}``). Without a signer only public endpoints work.
    """

    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        max_requests_per_second: float = 4.0,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        *,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError(f"base_url must be an https URL, got {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.host = parts.netloc.lower()
        self.signer = signer
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._sleep = sleep
        self._limiter = RateLimiter(max_requests_per_second, sleep=sleep, monotonic=monotonic)
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        signed = "signed" if self.signer is not None else "unsigned"
        return f"KalshiClient({self.base_url!r}, {signed})"

    # -- verbs

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request("POST", path, params=params, json=json)

    def delete(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request("DELETE", path, params=params, json=json)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request and return its JSON object (``{}`` for an empty body)."""
        method = method.upper()
        clean = _clean_params(params)
        attempt = 0
        while True:
            self._limiter.wait()
            request = self._http.build_request(method, path, params=clean, json=json)
            if self.signer is not None:
                request.headers.update(self.signer.headers(method, request.url.path))
            try:
                response = self._http.send(request)
            except httpx.TransportError as exc:
                if method in IDEMPOTENT_METHODS and attempt < self.max_retries:
                    log.info("%s %s: %s; retrying", method, path, type(exc).__name__)
                    self._sleep(self._backoff(attempt, None))
                    attempt += 1
                    continue
                raise KalshiExchangeError(
                    status=None,
                    code="transport_error",
                    message=type(exc).__name__,
                    method=method,
                    path=path,
                ) from exc
            status = response.status_code
            log.debug("%s %s -> %s", method, path, status)
            if self._should_retry(method, status) and attempt < self.max_retries:
                delay = self._backoff(attempt, response)
                log.info("%s %s -> %s; retrying in %.1fs", method, path, status, delay)
                self._sleep(delay)
                attempt += 1
                continue
            return self._result(method, path, response)

    # -- pagination

    def pages(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_pages: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Yield every page of a cursor-paginated list, following ``cursor`` until it is empty.

        Some endpoints ignore a cursor they cannot decode and return the first
        page again, so a repeated cursor stops the walk with an error instead of
        looping forever.
        """
        base = dict(params or {})
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            page = self.get(path, {**base, "cursor": cursor} if cursor else base)
            yield page
            cursor = page.get("cursor") or None
            if cursor is None:
                return
            if cursor in seen:
                raise KalshiExchangeError(
                    status=None, code="cursor_loop", message="cursor repeated", path=path
                )
            seen.add(cursor)
        raise KalshiExchangeError(
            status=None, code="too_many_pages", message=f"more than {max_pages} pages", path=path
        )

    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None,
        key: str,
        *,
        max_pages: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Yield the items under ``key`` from every page of a cursor-paginated list."""
        for page in self.pages(path, params, max_pages=max_pages):
            yield from page.get(key) or []

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KalshiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals

    def _should_retry(self, method: str, status: int) -> bool:
        if status in RETRY_STATUSES_ANY_METHOD:
            return True
        return status in RETRY_STATUSES_IDEMPOTENT and method in IDEMPOTENT_METHODS

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        delay = self.backoff_seconds * (2**attempt)
        if response is not None:
            for header in ("retry-after", "x-ratelimit-reset"):
                value = response.headers.get(header)
                if value:
                    with contextlib.suppress(ValueError):
                        delay = max(delay, float(value))
        return min(delay, self.max_backoff_seconds)

    def _result(self, method: str, path: str, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if 200 <= status < 300:
            if not response.content.strip():
                return {}
            try:
                body = response.json()
            except ValueError as exc:
                raise KalshiExchangeError(
                    status=status, code="invalid_json", method=method, path=path
                ) from exc
            if not isinstance(body, dict):
                raise KalshiExchangeError(
                    status=status, code="unexpected_body", method=method, path=path
                )
            return body
        code, message, details = error_fields(response)
        if status == 404:
            raise KalshiNotFound(
                code=code, message=message, details=details, method=method, path=path
            )
        raise KalshiExchangeError(
            status=status, code=code, message=message, details=details, method=method, path=path
        )


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop ``None`` values (httpx would send them as empty strings) and spell booleans."""
    if not params:
        return None
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        clean[key] = ("true" if value else "false") if isinstance(value, bool) else value
    return clean or None


__all__ = [
    "DEFAULT_USER_AGENT",
    "KalshiClient",
    "KalshiExchangeError",
    "KalshiNotFound",
    "RateLimiter",
    "error_fields",
]
