"""The evaluation rows refuse values the math could not score."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pmfa.evaluation.rows import ForecastRow

from .factories import T0, frow


def test_a_row_needs_a_horizon_or_a_trigger():
    with pytest.raises(ValidationError, match="horizon_days or a trigger_id"):
        ForecastRow(
            run_id="r",
            system="pipeline",
            event_ticker="E",
            market_ticker="E-1",
            category="cpi",
            as_of=T0,
            probability=0.5,
            market_price=0.5,
            outcome=True,
        )


def test_probabilities_and_prices_stay_in_the_unit_interval():
    with pytest.raises(ValidationError, match="probability"):
        frow(1.2, 0.5, True)
    with pytest.raises(ValidationError, match="market_price"):
        frow(0.5, -0.1, True)


def test_an_abstention_has_no_probability():
    assert frow(None, 0.5, True).abstained
    assert not frow(0.5, 0.5, True).abstained
