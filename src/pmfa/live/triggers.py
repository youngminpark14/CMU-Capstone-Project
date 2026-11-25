"""Triggers for the live modes: what starts an episode, as ``Trigger`` records with deterministic ids.

Three kinds, per the spec's episode flow (step 1):

- **New document.** A document from a followed source (a primary source of an
  enabled series) that is new since the last look. One trigger per document:
  id from the document id, ``at`` its publish time.
- **Price move.** On a market of a followed, open event, the YES mid moved by
  ``config.triggers.price_move_cents`` or more within ``price_move_window_minutes``,
  read from one-minute candles. Each move is measured from an anchor: at each
  candle end ``t`` the mid is compared with every mid in the window before it
  (the candles ending in ``(t - window, t)``, and the price at ``t - window``,
  which is the last candle at or before it) that is not older than the event's
  last price-move trigger, the anchor (each market's price at the anchor being
  its last candle at or before it). A crossing triggers, and the event's anchor moves to
  its time, so one step fires once even while its starting price stays inside
  the window (or across the edge of a clock bucket), while a second step, a
  reversal, or a sustained trend fires again each time it adds up to the
  threshold. One trigger per event per crossing, not per market: when markets
  of one event cross at the same candle end, the first ticker names it. Id from
  the event and the crossing time. Moves are reported from the start of the
  previous epoch-aligned window-length bucket, so a move in the last minutes of
  a bucket is not lost between two passes; ``anchors`` (the last trigger per
  event, which the live loop keeps in its cursor file) make the chain of
  crossings the same from pass to pass.
- **Schedule tick.** With ``config.triggers.daily_recheck_open_positions``, one
  re-check a day of each followed event the agent holds a position in: id from
  the event and the trading day, ``at`` the start of the trading day in
  ``config.oversight.timezone``.

Every id and every ``at`` is a function of data that does not change once
seen (a document, closed candles and the event's last price-move trigger, a
calendar day), so scanning twice yields the same triggers, and the controller, whose episode id is a function of the
trigger id and ``at``, returns the stored episode instead of running it again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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


def _priced(candles: Sequence[Candle]) -> list[tuple[datetime, Decimal]]:
    """``(end_ts, mid)`` of every candle with a mid, oldest first."""
    points = []
    for candle in sorted(candles, key=lambda c: c.end_ts):
        mid = candle_mid(candle)
        if mid is not None:
            points.append((candle.end_ts, mid))
    return points


def _move_at(
    points: Sequence[tuple[datetime, Decimal]],
    i: int,
    window: timedelta,
    anchor: datetime | None = None,
) -> Decimal:
    """The largest distance from the mid at ``points[i]`` to a mid in the window before it.

    The window is the points ending in ``(t - window, t)`` plus the price at
    ``t - window`` (the last point at or before it). With an ``anchor``, a move is
    measured from the prices seen since the last trigger: the points after it plus
    the market's price at the anchor, which is its last point at or before it (a
    thin market often has no priced candle at the anchor itself). Nothing older
    counts, and a point at or before the anchor does not move.
    """
    t, mid = points[i]
    move = Decimal(0)
    if anchor is not None and t <= anchor:
        return move
    for at, earlier in reversed(points[:i]):
        move = max(move, abs(mid - earlier))
        if at <= t - window:
            break  # the price at the window's start: nothing older counts
        if anchor is not None and at <= anchor:
            break  # the price at the anchor: nothing older counts
    return move


PriceMove = tuple[datetime, str, Decimal]
"""``(crossing time, market ticker, size of the move then)``."""


def price_moves(
    candles: Mapping[str, Sequence[Candle]],
    *,
    window: timedelta,
    threshold: Decimal,
    anchor: datetime | None = None,
) -> list[PriceMove]:
    """Every crossing of ``threshold`` among one event's markets, oldest first.

    ``candles`` maps each market of the event to its one-minute candles. At each
    candle end, in time order, a market whose mid differs by ``threshold`` or
    more from a mid in its window (see ``_move_at``) not older than ``anchor``
    crosses; the crossing moves ``anchor`` to that time, for every market of the
    event. Markets crossing at the same time give one crossing, named by the
    first ticker. ``anchor`` starts at the event's last trigger, if known.
    """
    series = {ticker: _priced(c) for ticker, c in sorted(candles.items())}
    index = {ticker: {at: i for i, (at, _) in enumerate(pts)} for ticker, pts in series.items()}
    times = sorted({at for pts in series.values() for at, _ in pts})
    found: list[PriceMove] = []
    for t in times:
        for ticker, pts in series.items():
            i = index[ticker].get(t)
            if i is None:
                continue
            move = _move_at(pts, i, window, anchor)
            if move >= threshold:
                found.append((t, ticker, move))
                anchor = t
                break
    return found


def first_crossing(
    candles: Sequence[Candle], *, start: datetime, window: timedelta, threshold: Decimal
) -> tuple[datetime, Decimal] | None:
    """The first crossing of one market in ``(start, start + window]``, and its size.

    The chain of crossings (``price_moves``) is run over all of ``candles``,
    which must reach back far enough to price the window before the first
    candle of interest.
    """
    for at, _, move in price_moves({"": candles}, window=window, threshold=threshold):
        if start < at <= start + window:
            return at, move
        if at > start + window:
            break
    return None


def price_move_trigger(event_ticker: str, at: datetime, detail: str) -> Trigger:
    """The trigger for a crossing at ``at``; its id comes from the event and that time."""
    return Trigger(
        trigger_id=stable_id(
            "trigger", TriggerKind.PRICE_MOVE.value, event_ticker, at.astimezone(UTC).isoformat()
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
    anchors: Mapping[str, datetime] | None = None,
) -> list[Trigger]:
    """Price-move triggers for the followed, open ``events``, one per event per crossing.

    Reads one-minute candles for each open market from two windows before the
    previous bucket up to ``now``, and reports the crossings from the start of
    the previous bucket. ``anchors`` maps an event to the time of its last
    price-move trigger (see the module docstring). A market whose candles
    cannot be read is skipped and noted.
    """
    window = timedelta(minutes=config.triggers.price_move_window_minutes)
    threshold = Decimal(config.triggers.price_move_cents) / 100
    report_from = bucket_start(now, window) - window
    fetch_from = report_from - 2 * window
    followed = {s.ticker for s in config.follow.enabled_series()}
    known = anchors or {}
    triggers: list[Trigger] = []
    for event in events:
        if event.series_ticker not in followed:
            continue
        candles: dict[str, list[Candle]] = {}
        for market in event.markets:
            if market.status is not MarketStatus.OPEN:
                continue
            try:
                candles[market.ticker] = exchange.get_candles(
                    market.ticker, start=fetch_from, end=now, interval_minutes=CANDLE_MINUTES
                )
            except (ExchangeError, NotFound) as exc:
                if notes is not None:
                    notes.append(f"candles for {market.ticker} not read: {exc}")
        moves = price_moves(
            candles, window=window, threshold=threshold, anchor=known.get(event.event_ticker)
        )
        for at, ticker, move in moves:
            if not report_from < at <= now:
                continue
            detail = (
                f"{ticker} YES mid moved {move * 100:.1f}c within "
                f"{config.triggers.price_move_window_minutes} min (at {at:%H:%M} UTC)"
            )
            triggers.append(price_move_trigger(event.event_ticker, at, detail))
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
    "price_moves",
    "schedule_trigger",
    "schedule_triggers",
]
