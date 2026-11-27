"""Configuration limits that protect the as-of rule in fills."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pmfa.config import Config


def test_a_negative_backtest_latency_is_refused():
    with pytest.raises(ValidationError, match="backtest_latency_seconds"):
        Config.model_validate({"execution": {"backtest_latency_seconds": -3600}})


def test_zero_backtest_latency_is_allowed():
    config = Config.model_validate({"execution": {"backtest_latency_seconds": 0}})
    assert config.execution.backtest_latency_seconds == 0
