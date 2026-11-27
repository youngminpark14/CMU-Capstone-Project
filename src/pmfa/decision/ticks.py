"""Valid limit prices.

A market lists its valid YES prices in ``price_ranges``: within each range,
prices from ``start`` to ``end`` in steps of ``step``. A NO price ``q`` is valid
when the YES price ``1 - q`` is, because buying NO is selling YES on the same
book. Prices of exactly 0 or 1 are never valid limit prices.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from decimal import Decimal
from functools import lru_cache

from pmfa.records.base import CENT, ONE, ZERO
from pmfa.records.market import Market, PriceRange, Side

DEFAULT_PRICE_RANGES: tuple[PriceRange, ...] = (PriceRange(start=CENT, end=ONE - CENT, step=CENT),)
"""The one-cent grid from 1 to 99 cents, used when a market lists no ranges."""

Ranges = Market | Sequence[PriceRange]
"""A market, or its price ranges directly."""


@lru_cache(maxsize=256)
def _yes_ticks(ranges: tuple[tuple[Decimal, Decimal, Decimal], ...]) -> tuple[Decimal, ...]:
    ticks: set[Decimal] = set()
    for start, end, step in ranges:
        if step <= ZERO:
            raise ValueError(f"price range step must be positive, got {step}")
        price = start
        while price <= end:
            if ZERO < price < ONE:
                ticks.add(price)
            price += step
    return tuple(sorted(ticks))


def _ranges(source: Ranges) -> tuple[tuple[Decimal, Decimal, Decimal], ...]:
    listed = source.price_ranges if isinstance(source, Market) else source
    chosen = listed or DEFAULT_PRICE_RANGES
    return tuple((r.start, r.end, Decimal(r.step)) for r in chosen)


def valid_ticks(source: Ranges, side: Side = Side.YES) -> tuple[Decimal, ...]:
    """Every valid limit price for ``side``, ascending."""
    yes = _yes_ticks(_ranges(source))
    if side is Side.YES:
        return yes
    return tuple(ONE - t for t in reversed(yes))


def is_valid_tick(price: Decimal, source: Ranges, side: Side = Side.YES) -> bool:
    """True when ``price`` is a valid limit price for ``side``."""
    ticks = valid_ticks(source, side)
    i = bisect_left(ticks, price)
    return i < len(ticks) and ticks[i] == price


def snap_down(price: Decimal, source: Ranges, side: Side = Side.YES) -> Decimal | None:
    """The highest valid tick at or below ``price``; ``None`` when there is none."""
    ticks = valid_ticks(source, side)
    i = bisect_right(ticks, price)
    return ticks[i - 1] if i > 0 else None


def snap_up(price: Decimal, source: Ranges, side: Side = Side.YES) -> Decimal | None:
    """The lowest valid tick at or above ``price``; ``None`` when there is none."""
    ticks = valid_ticks(source, side)
    i = bisect_left(ticks, price)
    return ticks[i] if i < len(ticks) else None
