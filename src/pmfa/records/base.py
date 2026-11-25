"""Building blocks shared by every record.

A record is an immutable Pydantic model that crosses a module boundary. Records
hold data only. Money and prices are decimals, times are timezone-aware UTC.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict

PRICE_QUANTUM = Decimal("0.0001")
"""Prices are fixed-point dollars to four decimals, as the exchange sends them."""

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


class Record(BaseModel):
    """Base class for records: frozen, and unknown fields are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime: records hold timezone-aware times")
    return value.astimezone(UTC)


def _to_decimal(value: Any) -> Any:
    # Floats go through str() so 0.1 becomes Decimal("0.1"), not its binary expansion.
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _check_price(value: Decimal) -> Decimal:
    if not (ZERO <= value <= ONE):
        raise ValueError(f"price {value} is outside [0, 1]")
    return value.quantize(PRICE_QUANTUM)


UtcTime = Annotated[datetime, AfterValidator(_to_utc)]
"""A timezone-aware datetime, normalized to UTC."""

Dollars = Annotated[Decimal, BeforeValidator(_to_decimal)]
"""A dollar amount as a decimal."""

Price = Annotated[Decimal, BeforeValidator(_to_decimal), AfterValidator(_check_price)]
"""A contract price in dollars within [0, 1], quantized to four decimals."""

Count = Annotated[Decimal, BeforeValidator(_to_decimal)]
"""A contract count as the exchange reports it (fixed-point, two decimals)."""


class RunMode(StrEnum):
    """A run mode is a choice of world: what sits behind the episode's ports."""

    REPLAY = "replay"
    BACKTEST = "backtest"
    SHADOW = "shadow"
    DEMO = "demo"


class Stage(StrEnum):
    """The autonomy level. Order matters: each stage is earned from the one before."""

    BACKTEST = "backtest"
    SHADOW = "shadow"
    APPROVAL = "approval"
    AUTONOMOUS = "autonomous"

    @property
    def rank(self) -> int:
        return list(Stage).index(self)


def stable_id(prefix: str, *parts: object) -> str:
    """A deterministic id: the same parts always give the same id.

    Reruns of a backtest therefore reuse episode, node, and order ids, which is
    what makes cached responses and idempotent order ids line up.
    """
    digest = hashlib.blake2b("\x1f".join(str(p) for p in parts).encode(), digest_size=8)
    return f"{prefix}-{digest.hexdigest()}"
