"""The origin rule: whether the evidence behind a forecast is enough to trade on.

Confidence itself is computed with the forecast, in ``pmfa.forecast.aggregate``.
"""

from __future__ import annotations

from collections.abc import Sequence

from pmfa.records.evidence import Origin

MIN_SECONDARY_ORIGINS = 2
"""A trade without a primary origin needs this many independent secondary origins."""


def origin_rule_met(origins: Sequence[Origin]) -> bool:
    """One primary origin, or two independent secondary origins.

    Origins arrive already merged (``Knowledge.origins`` returns independent
    ones), so distinct origin ids count as independent.
    """
    if any(o.is_primary for o in origins):
        return True
    secondary = {o.origin_id for o in origins if not o.is_primary}
    return len(secondary) >= MIN_SECONDARY_ORIGINS
