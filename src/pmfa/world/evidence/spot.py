"""A price snapshot from Coinbase's public market data, as one evidence document.

``pmfa ask`` reads it for a series that names a ``spot_product`` (the Bitcoin
series names ``BTC-USD``). Three requests through the polite fetcher, each
fresh (``refresh=True``), no key:

- ``GET /products/{product}/ticker``: the last trade's price and time;
- ``GET /products/{product}/candles?granularity=60``: one-minute rows
  ``[time, low, high, open, close, volume]``, newest first, ``time`` the start
  of the minute in epoch seconds;
- the same with ``granularity=3600``: hourly rows.

The document says in plain sentences what a forecaster of an hourly price market
needs: the last price; the price 15 minutes, 60 minutes and 24 hours earlier
with the change; the high and low of the last hour and the last day; how much
the price moves a minute, scaled to 15 and 60 minutes; and the closes at
five-minute steps over the last hour. It uses the words a query about the market
would (Bitcoin, BTC, price, U.S. dollars), since the default embedder matches
words. Its ``published_at`` is the clock's time after the last read, so nothing
in it postdates it, and rows dated after that time are left out.

The price of one exchange is not the market's settlement price: Kalshi's Bitcoin
markets resolve on CF Benchmarks' Bitcoin Real-Time Index, an average over
several exchanges, and the document says so. The market's own price is never in
it: a role must not see it.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pmfa.errors import PmfaError
from pmfa.records.evidence import Document
from pmfa.world.evidence.common import make_document

if TYPE_CHECKING:
    from pmfa.ports import Clock
    from pmfa.world.evidence.http import Fetcher

COINBASE_SOURCE = "coinbase.com"
SPOT_KIND = "spot_price"
NAMES = {"BTC": "Bitcoin"}
"""The plain name of the one configured base currency; another product is named by its code."""


class SpotError(PmfaError):
    """Coinbase answered, but not with the price data the snapshot needs."""


@dataclass(frozen=True)
class Row:
    """One candle: when it started, its close, its low and its high."""

    start: datetime
    close: Decimal
    low: Decimal
    high: Decimal


def _json(fetcher: Fetcher, url: str) -> Any:
    try:
        return json.loads(fetcher.get(url, refresh=True).text, parse_float=Decimal)
    except ValueError as exc:
        raise SpotError(f"{url} did not answer with JSON: {exc}") from None


def _rows(raw: Any, seconds: int, now: datetime) -> list[tuple[datetime, Row]]:
    """``(end, row)`` pairs, oldest first, for rows that started by ``now``."""
    if not isinstance(raw, list):
        raise SpotError("the candles answer is not a list of rows")
    out = []
    for item in raw:
        try:
            start = datetime.fromtimestamp(int(item[0]), tz=UTC)
            low, high, close = (Decimal(str(item[i])) for i in (1, 2, 4))
        except (TypeError, ValueError, IndexError, ArithmeticError):
            raise SpotError(
                f"a candle row is not [time, low, high, open, close, volume]: {item!r}"
            ) from None
        if start <= now:
            out.append((start + timedelta(seconds=seconds), Row(start, close, low, high)))
    return sorted(out, key=lambda pair: pair[0])


def _at(rows: list[tuple[datetime, Row]], moment: datetime) -> tuple[datetime, Decimal] | None:
    """The close of the last row that ended at or before ``moment``, with that end time."""
    found = None
    for end, row in rows:
        if end <= moment:
            found = (end, row.close)
    return found


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _change(now: Decimal, then: Decimal) -> str:
    diff = now - then
    sign = "+" if diff >= 0 else "-"
    percent = (diff / then * 100) if then else Decimal(0)
    return f"{sign}{_money(abs(diff))} ({percent:+.2f}%)"


def _hhmm(moment: datetime) -> str:
    return f"{moment:%H:%M} UTC"


def _day(moment: datetime) -> str:
    return f"{moment:%B} {moment.day}, {moment.year}"


def snapshot_document(fetcher: Fetcher, product: str, *, base_url: str, clock: Clock) -> Document:
    """The live price snapshot of ``product`` as a Document (see the module docstring).

    Raises ``FetchError`` when Coinbase cannot be reached or refuses, and
    :class:`SpotError` when its answer lacks what the snapshot needs.
    """
    base = base_url.rstrip("/")
    ticker_url = f"{base}/products/{product}/ticker"
    ticker = _json(fetcher, ticker_url)
    minute_raw = _json(fetcher, f"{base}/products/{product}/candles?granularity=60")
    hour_raw = _json(fetcher, f"{base}/products/{product}/candles?granularity=3600")
    now = clock.now().astimezone(UTC)
    try:
        last = Decimal(str(ticker["price"]))
        traded_at = datetime.fromisoformat(str(ticker["time"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise SpotError(f"{ticker_url} gave no price and time") from None
    traded_at = min(traded_at.astimezone(UTC), now)
    minutes = _rows(minute_raw, 60, now)
    hours = _rows(hour_raw, 3600, now)
    if not minutes or not hours:
        raise SpotError(f"Coinbase gave no recent candles for {product}")

    name = NAMES.get(product.split("-")[0].upper(), product.split("-")[0])
    code = product.split("-")[0].upper()
    title = f"{name} ({product}) price on Coinbase at {_hhmm(now)}, {_day(now)}"
    lines = [
        f"The last {name} ({code}) trade on Coinbase ({product}) was at {_money(last)} "
        f"U.S. dollars, at {traded_at:%H:%M:%S} UTC on {_day(traded_at)}."
    ]
    for label, back, rows, period in (
        ("Fifteen minutes", timedelta(minutes=15), minutes, "minute"),
        ("Sixty minutes", timedelta(minutes=60), minutes, "minute"),
        ("About 24 hours", timedelta(hours=24), hours, "hour"),
    ):
        found = _at(rows, now - back)
        if found is None:
            lines.append(f"{label} earlier: the {name} price is not in the data read.")
            continue
        end, then = found
        lines.append(
            f"{label} earlier (the close of the {period} ending {_hhmm(end)} on {_day(end)}) "
            f"the {name} price was {_money(then)}; the {code} price has changed by "
            f"{_change(last, then)} since."
        )
    for label, rows in (
        ("60 minutes", [row for end, row in minutes if end > now - timedelta(minutes=60)]),
        (
            "24 hours",
            [row for end, row in hours if end > now - timedelta(hours=24)]
            + [row for _, row in minutes],
        ),
    ):
        if rows:
            low, high = min(r.low for r in rows), max(r.high for r in rows)
            lines.append(
                f"Over the last {label} the {name} price ranged from a low of {_money(low)} to "
                f"a high of {_money(high)} U.S. dollars."
            )
    # Complete minutes of the last hour (not the one still forming), and a return only between
    # minutes 60 seconds apart: Coinbase leaves out a minute with no trades.
    hour = [row for end, row in minutes if now - timedelta(minutes=60) <= end <= now]
    returns = [
        float(b.close / a.close - 1)
        for a, b in itertools.pairwise(hour)
        if a.close and b.start - a.start == timedelta(seconds=60)
    ]
    if len(returns) >= 2:
        sd = statistics.stdev(returns)
        per15, per60 = sd * math.sqrt(15), sd * math.sqrt(60)
        lines.append(
            f"The standard deviation of one-minute {code} price returns over the last 60 minutes "
            f"was {sd * 100:.3f}%. At that rate a typical move is about {per15 * 100:.2f}% "
            f"(about {_money(last * Decimal(str(per15)))}) over 15 minutes and "
            f"{per60 * 100:.2f}% (about {_money(last * Decimal(str(per60)))}) over 60 minutes."
        )
    steps = []
    for k in range(12, -1, -1):
        found = _at(minutes, now - timedelta(minutes=5 * k))
        if found is not None and (not steps or steps[-1][0] != found[0]):
            steps.append(found)
    if steps:
        lines.append(
            f"{name} price closes at five-minute steps over the last hour, oldest first: "
            + ", ".join(f"{_hhmm(end)} {_money(close)}" for end, close in steps)
            + "."
        )
    lines.append(
        f"Kalshi's {name} price markets resolve on CF Benchmarks' {name} Real-Time Index, an "
        f"average over several exchanges including Coinbase, so this one exchange's {code} price "
        "can differ from it by a few dollars."
    )
    return make_document(
        doc_id=f"spot:{product}:{now:%Y%m%dT%H%M%SZ}",
        source=COINBASE_SOURCE,
        kind=SPOT_KIND,
        url=ticker_url,
        title=title,
        published_at=now,
        text="\n\n".join(lines) + "\n",
    )


__all__ = ["COINBASE_SOURCE", "SPOT_KIND", "SpotError", "snapshot_document"]
