"""Calibration arithmetic over resolved ledger entries.

Kept apart from the store so the numbers can be checked on hand-computed cases.
"""

from __future__ import annotations

from collections.abc import Sequence

N_BINS = 10


def brier(probability: float, outcome: bool) -> float:
    """Squared error of a probability against a yes/no outcome."""
    return (probability - (1.0 if outcome else 0.0)) ** 2


def expected_calibration_error(
    pairs: Sequence[tuple[float, bool]], n_bins: int = N_BINS
) -> float | None:
    """Expected calibration error over ``n_bins`` equal-width probability bins.

    Each forecast falls in bin ``floor(p * n_bins)`` (1.0 goes in the top bin).
    The error is the forecast-weighted mean, over non-empty bins, of
    ``|mean probability - observed frequency|``. ``None`` when there are no pairs.
    """
    if not pairs:
        return None
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for probability, outcome in pairs:
        index = min(int(probability * n_bins), n_bins - 1)
        bins[max(index, 0)].append((probability, outcome))
    total = len(pairs)
    error = 0.0
    for members in bins:
        if not members:
            continue
        mean_p = sum(p for p, _ in members) / len(members)
        frequency = sum(1 for _, o in members if o) / len(members)
        error += len(members) / total * abs(mean_p - frequency)
    return error
