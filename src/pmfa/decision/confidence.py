"""Confidence and the origin rule: how much the evidence behind a forecast is worth."""

from __future__ import annotations

from collections.abc import Sequence

from pmfa.config import Config
from pmfa.records.evidence import Origin

MIN_SECONDARY_ORIGINS = 2
"""A trade without a primary origin needs this many independent secondary origins."""


def confidence(
    coverage: float, independent_origins: int, budget_exhausted: bool, config: Config
) -> float:
    """``coverage x min(1, origins / full_origins)``, halved when the budget ran out.

    The result is in [0, 1] and multiplies the Kelly size, so it can only reduce it.
    """
    full = config.forecast.confidence_full_origins
    origin_factor = 1.0 if full <= 0 else min(1.0, max(0, independent_origins) / full)
    value = min(1.0, max(0.0, coverage)) * origin_factor
    if budget_exhausted:
        value /= 2
    return value


def origin_rule_met(origins: Sequence[Origin]) -> bool:
    """One primary origin, or two independent secondary origins.

    Origins arrive already merged (``Knowledge.origins`` returns independent
    ones), so distinct origin ids count as independent.
    """
    if any(o.is_primary for o in origins):
        return True
    secondary = {o.origin_id for o in origins if not o.is_primary}
    return len(secondary) >= MIN_SECONDARY_ORIGINS
