"""Triggers for the live modes: what starts an episode, as ``Trigger`` records with deterministic ids.

Three kinds, per the spec's episode flow (step 1):

- **New document.** A document from a followed source (a primary source of an
  enabled series) that is new since the last look. One trigger per document:
  id from the document id, ``at`` its publish time.
- **Price move.** On a market of a followed, open event, the YES mid moved by
  ``config.triggers.price_move_cents`` or more within a window of
  ``price_move_window_minutes``, read from one-minute candles. Windows are fixed
  buckets of that length aligned to the Unix epoch; within a bucket the move is
  measured from the price at the bucket's start (the last candle at or before
  it) through each candle in the bucket. One trigger per event per bucket, not
  per market: ``at`` is the end of the first candle at which any of the event's
  markets crossed, id from the event and the bucket. The current and the
  previous bucket are scanned, so a move in the last minutes of a bucket is not
  lost between two passes.
- **Schedule tick.** With ``config.triggers.daily_recheck_open_positions``, one
  re-check a day of each followed event the agent holds a position in: id from
  the event and the trading day, ``at`` the start of the trading day in
  ``config.oversight.timezone``.

Every id and every ``at`` is a function of data that does not change once
seen (a document, closed candles, a calendar day), so scanning twice yields the
same triggers, and the controller, whose episode id is a function of the
trigger id and ``at``, returns the stored episode instead of running it again.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pmfa.config import Config
from pmfa.errors import ExchangeError, NotFound
from pmfa.ports import Exchange, Executor
from pmfa.records.base import ONE, stable_id
from pmfa.records.evidence import Document, Trigger, TriggerKind
from pmfa.records.market import Candle, Event, MarketStatus

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CANDLE_MINUTES = 1


# --------------------------------------------------------------------------- documents


def followed_sources(config: Config) -> frozenset[str]:
    """Publishers whose new documents start episodes: the primary sources of the enabled series."""
    return frozenset(src for s in config.follow.enabled_series() for src in s.primary_sources)


def document_trigger(document: Document) -> Trigger:
    return Trigger(
        trigger_id=stable_id("trigger", TriggerKind.NEW_DOCUMENT.value, document.doc_id),
        kind=TriggerKind.NEW_DOCUMENT,
        at=document.published_at,
        doc_id=document.doc_id,
        detail=f"{document.source}: {document.title}",
    )


def document_triggers(documents: Iterable[Document], config: Config) -> list[Trigger]:
    """One trigger per new document from a followed source, oldest first."""
    sources = followed_sources(config)
    unique = {d.doc_id: d for d in documents if d.source in sources}
    ordered = sorted(unique.values(), key=lambda d: (d.published_at, d.doc_id))
    return [document_trigger(d) for d in ordered]


# --------------------------------------------------------------------------- price moves


def bucket_start(moment: datetime, window: timedelta) -> datetime:
    """Start of the epoch-aligned bucket of length ``window`` that holds ``moment``."""
    elapsed = moment.astimezone(UTC) - EPOCH
    return EPOCH + (elapsed // window) * window


def candle_mid(candle: Candle) -> Decimal | None:
    """The YES mid at the candle's close, when both sides were quoted inside (0, 1)."""
    bid, ask = candle.yes_bid, candle.yes_ask
    if bid is None or ask is None:
        return None
    if not (bid.close > 0 and ask.close < ONE):
        return None
    return (bid.close + ask.close) / 2


def first_crossing(
    candles: Sequence[Candle], *, start: datetime, window: timedelta, threshold: Decimal
) -> tuple[datetime, Decimal] | None:
    """The first candle end in ``(start, start + window]`` at which the mid's range reached ``threshold``.

    The range starts from the price at ``start`` (the last priced candle ending
    at or before it), so a jump at the bucket's first minute counts. Returns the
    crossing time and the range at that moment.
    """
    end = start + window
    ordered = sorted(candles, key=lambda c: c.end_ts)
    low: Decimal | None = None
    high: Decimal | None = None
    for candle in ordered:
        if candle.end_ts > start:
            break
        mid = candle_mid(candle)
        if mid is not None:
            low = high = mid
    for candle in ordered:
        if not (start < candle.end_ts <= end):
            continue
        mid = candle_mid(candle)
        if mid is None:
            continue
        low = mid if low is None else min(low, mid)
        high = mid if high is None else max(high, mid)
        if high - low >= threshold:
            return candle.end_ts, high - low
    return None


def price_move_trigger(event_ticker: str, bucket: datetime, at: datetime, detail: str) -> Trigger:
    return Trigger(
        trigger_id=stable_id(
            "trigger", TriggerKind.PRICE_MOVE.value, event_ticker, bucket.isoformat()
        ),
        kind=TriggerKind.PRICE_MOVE,
        at=at,
        event_ticker=event_ticker,
        detail=detail,
    )


def price_move_triggers(
    exchange: Exchange,
    events: Iterable[Event],
    config: Config,
    now: datetime,
    *,
    notes: list[str] | None = None,
) -> list[Trigger]:
    """Price-move triggers for the followed, open ``events``, one per event per bucket.

    Reads one-minute candles for each open market over the previous bucket, the
    current one, and one window before them (for the starting price). A market
    whose candles cannot be read is skipped and noted.
    """
    window = timedelta(minutes=config.triggers.price_move_window_minutes)
    threshold = Decimal(config.triggers.price_move_cents) / 100
    current = bucket_start(now, window)
    buckets = [current - window, current]
    fetch_from = buckets[0] - window
    followed = {s.ticker for s in config.follow.enabled_series()}
    triggers: list[Trigger] = []
    for event in events:
        if event.series_ticker not in followed:
            continue
        crossings: dict[datetime, tuple[datetime, str, Decimal]] = {}
        for market in event.markets:
            if market.status is not MarketStatus.OPEN:
                continue
            try:
                candles = exchange.get_candles(
                    market.ticker, start=fetch_from, end=now, interval_minutes=CANDLE_MINUTES
                )
            except (ExchangeError, NotFound) as exc:
                if notes is not None:
                    notes.append(f"candles for {market.ticker} not read: {exc}")
                continue
            for bucket in buckets:
                found = first_crossing(candles, start=bucket, window=window, threshold=threshold)
                if found is None:
                    continue
                at, move = found
                best = crossings.get(bucket)
                if best is None or (at, market.ticker) < (best[0], best[1]):
                    crossings[bucket] = (at, market.ticker, move)
        for bucket, (at, ticker, move) in sorted(crossings.items()):
            detail = (
                f"{ticker} YES mid moved {move * 100:.1f}c within "
                f"{config.triggers.price_move_window_minutes} min (bucket from {bucket:%H:%M} UTC)"
            )
            triggers.append(price_move_trigger(event.event_ticker, bucket, at, detail))
    return triggers


# --------------------------------------------------------------------------- schedule ticks


def schedule_trigger(event_ticker: str, day_start: datetime, timezone: str) -> Trigger:
    from pmfa.decision import trading_day_start

    day = trading_day_start(day_start, timezone)
    return Trigger(
        trigger_id=stable_id("trigger", TriggerKind.SCHEDULE.value, event_ticker, day.isoformat()),
        kind=TriggerKind.SCHEDULE,
        at=day,
        event_ticker=event_ticker,
        detail="daily re-check of an open position",
    )


def schedule_triggers(
    executor: Executor,
    config: Config,
    now: datetime,
    *,
    open_events: Iterable[str] | None = None,
) -> list[Trigger]:
    """The daily re-check of each event with an open position, when the configuration asks for it.

    ``open_events``, when given, limits the re-checks to those events (the
    followed events that are still open).
    """
    if not config.triggers.daily_recheck_open_positions:
        return []
    allowed = set(open_events) if open_events is not None else None
    followed = {s.ticker for s in config.follow.enabled_series()}
    held = sorted(
        {
            p.event_ticker
            for p in executor.positions()
            if p.contracts > 0 and p.event_ticker.split("-", 1)[0] in followed
        }
    )
    tz = config.oversight.timezone
    return [schedule_trigger(e, now, tz) for e in held if allowed is None or e in allowed]


# --------------------------------------------------------------------------- all kinds


def combine(*groups: Iterable[Trigger]) -> list[Trigger]:
    """All triggers once each (by id), in time order."""
    unique: dict[str, Trigger] = {}
    for group in groups:
        for t in group:
            unique.setdefault(t.trigger_id, t)
    return sorted(unique.values(), key=lambda t: (t.at, t.trigger_id))


__all__ = [
    "bucket_start",
    "candle_mid",
    "combine",
    "document_trigger",
    "document_triggers",
    "first_crossing",
    "followed_sources",
    "price_move_trigger",
    "price_move_triggers",
    "schedule_trigger",
    "schedule_triggers",
]
