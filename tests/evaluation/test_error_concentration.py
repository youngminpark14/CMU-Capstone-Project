"""Where a system's squared error comes from, on a fixture small enough to add up by hand."""

from __future__ import annotations

import pytest

from pmfa.config import Config
from pmfa.evaluation.run_metrics import compute_metrics, error_concentration

from .factories import frow

# Squared error (p - outcome)^2 per row, by hand:
#   EV-A-1  p 0.95, NO   0.9025   above 0.90
#   EV-A-2  p 0.05, NO   0.0025   below 0.10
#   EV-B-1  p 0.50, YES  0.25     0.10 to 0.90  (7 days before close)
#   EV-B-1  p 0.60, YES  0.16     0.10 to 0.90  (1 day before close)
#   EV-B-2  p 0.85, YES  0.0225   0.10 to 0.90
#   EV-C-1  p 0.10, NO   0.01     0.10 to 0.90  (0.10 itself is in the middle band)
#   EV-C-2  p 0.90, YES  0.01     0.10 to 0.90  (0.90 itself is in the middle band)
#   EV-C-1  abstained            not in the pooled answered Brier, so not counted
# Total 0.9025 + 0.0025 + 0.25 + 0.16 + 0.0225 + 0.01 + 0.01 = 1.3575 over 7 rows.
# Bands: below 0.0025 / 1.3575 = 0.0018 (1 row); middle 0.4525 / 1.3575 = 0.3333 (5 rows);
#        above 0.9025 / 1.3575 = 0.6648 (1 row).
# Events: EV-A 0.905 / 1.3575 = 0.6667 (2 rows); EV-B 0.4325; EV-C 0.02.
# Markets: EV-A-1 0.9025 (0.6648); EV-B-1 0.41 (0.3020, 2 rows); EV-B-2 0.0225 (0.0166).
ROWS = [
    frow(0.95, 0.95, False, event="EV-A", market="EV-A-1"),
    frow(0.05, 0.05, False, event="EV-A", market="EV-A-2"),
    frow(0.50, 0.50, True, event="EV-B", market="EV-B-1", horizon_days=7),
    frow(0.60, 0.60, True, event="EV-B", market="EV-B-1", horizon_days=1),
    frow(0.85, 0.85, True, event="EV-B", market="EV-B-2"),
    frow(0.10, 0.10, False, event="EV-C", market="EV-C-1"),
    frow(0.90, 0.90, True, event="EV-C", market="EV-C-2"),
    frow(None, 0.30, False, event="EV-C", market="EV-C-1", horizon_days=7),
]
TOTAL = 1.3575


def test_the_error_is_split_by_band_event_and_market() -> None:
    block = error_concentration(ROWS)
    assert block is not None
    assert block.n_rows == 7
    assert block.squared_error == pytest.approx(TOTAL)
    bands = {b.name: (b.n_rows, b.squared_error, b.share) for b in block.bands}
    assert list(bands) == ["below 0.10", "0.10 to 0.90", "above 0.90"]
    assert bands["below 0.10"] == (1, pytest.approx(0.0025), pytest.approx(0.0025 / TOTAL))
    assert bands["0.10 to 0.90"] == (5, pytest.approx(0.4525), pytest.approx(0.4525 / TOTAL))
    assert bands["above 0.90"] == (1, pytest.approx(0.9025), pytest.approx(0.9025 / TOTAL))
    assert (block.top_event.name, block.top_event.n_rows) == ("EV-A", 2)
    assert block.top_event.share == pytest.approx(0.905 / TOTAL)
    assert [(m.name, m.n_rows) for m in block.top_markets] == [
        ("EV-A-1", 1),
        ("EV-B-1", 2),
        ("EV-B-2", 1),
    ]
    assert [m.share for m in block.top_markets] == [
        pytest.approx(0.9025 / TOTAL),
        pytest.approx(0.41 / TOTAL),
        pytest.approx(0.0225 / TOTAL),
    ]


def test_no_error_leaves_the_shares_undefined_and_no_rows_gives_no_block() -> None:
    perfect = error_concentration([frow(1.0, 0.9, True), frow(0.0, 0.1, False, market="EV-1-N")])
    assert perfect is not None and perfect.squared_error == 0
    assert all(b.share is None for b in perfect.bands)
    assert perfect.top_event.share is None
    assert error_concentration([frow(None, 0.5, True)]) is None


def test_the_block_sits_beside_each_scored_systems_pooled_metrics() -> None:
    market = [
        r.model_copy(update={"system": "market", "probability": r.market_price}) for r in ROWS
    ]
    abstained_only = [frow(None, 0.5, True, system="prompt_only")]
    # A news-triggered row of the trading pass (no horizon): outside the pooled
    # Brier score, so its squared error of 0.81 must not be counted.
    news = frow(0.9, 0.5, False, event="EV-N", market="EV-N-1").model_copy(
        update={"horizon_days": None}
    )
    rows = [*ROWS, *market, *abstained_only, news]
    metrics = compute_metrics(
        rows,
        ["" if r.probability is not None else "no_evidence: none" for r in rows],
        [],
        [],
        [],
        [],
        systems={"pipeline": "run", "market": "run", "prompt_only": "run"},
        config=Config(),
    )
    pipeline = metrics["systems"]["pipeline"]["error_concentration"]
    assert pipeline["n_rows"] == metrics["systems"]["pipeline"]["by_horizon"]["all"]["n_rows"] - 1
    assert pipeline["n_rows"] == 7
    assert (
        pipeline["n_rows"]
        == (metrics["systems"]["pipeline"]["by_horizon"]["all"]["answered"]["sample"]["n_rows"])
    )
    assert pipeline["squared_error"] == pytest.approx(TOTAL)
    assert "EV-N-1" not in [m["name"] for m in pipeline["top_markets"]]
    # The market is scored on all eight rows, its abstention-free price on EV-C-1 included.
    assert metrics["systems"]["market"]["error_concentration"]["n_rows"] == 8
    assert "error_concentration" not in metrics["systems"]["prompt_only"]
