"""What a model call costs, and the guard that keeps a backtest window after every model's cutoff.

Prices come from ``config.models.catalog`` (dollars per million tokens), so a
price change is a configuration diff. Dollars are decimals, never floats.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from pmfa.config import Config, ModelInfo, default_config
from pmfa.errors import ConfigError

_PER_MILLION = Decimal(1_000_000)


def cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    catalog: Mapping[str, ModelInfo] | None = None,
) -> Decimal:
    """Dollars for one call: tokens times the catalog's per-million prices, exact.

    ``catalog`` defaults to the built-in configuration's catalog. A model the
    catalog does not list is a configuration error: an unpriced call would slip
    past the dollar caps.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    table = catalog if catalog is not None else default_config().models.catalog
    info = table.get(model)
    if info is None:
        raise ConfigError(f"models.catalog has no price for {model}")
    return (
        Decimal(input_tokens) * info.input_per_mtok + Decimal(output_tokens) * info.output_per_mtok
    ) / _PER_MILLION


def assert_window_after_cutoffs(config: Config, window_start: date) -> dict[str, date]:
    """Refuse a backtest window that starts on or before any assigned model's knowledge cutoff.

    A model that may have read about the outcomes it is asked to forecast would
    leak the answer. Returns each assigned model's listed cutoff, for the run
    manifest. Raises ``ConfigError`` naming every offending slot.
    """
    cutoffs: dict[str, date] = {}
    problems: list[str] = []
    for slot, assignment in sorted(config.models.slots.items()):
        info = config.models.catalog.get(assignment.model)
        if info is None:
            problems.append(f"slot {slot.value}: {assignment.model} is not in models.catalog")
            continue
        cutoffs[assignment.model] = info.knowledge_cutoff
        if info.knowledge_cutoff >= window_start:
            problems.append(
                f"slot {slot.value}: {assignment.model} has knowledge cutoff "
                f"{info.knowledge_cutoff.isoformat()}, on or after the window start "
                f"{window_start.isoformat()}"
            )
    if problems:
        raise ConfigError("backtest window overlaps model knowledge: " + "; ".join(problems))
    return cutoffs


__all__ = ["assert_window_after_cutoffs", "cost"]
