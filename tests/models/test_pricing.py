from datetime import date
from decimal import Decimal

import pytest

from pmfa.errors import ConfigError
from pmfa.models.pricing import assert_window_after_cutoffs, cost

from .fixtures import make_config


def test_cost_is_exact_decimal_dollars():
    # Sonnet 5.5: $2 in, $10 out per million tokens.
    assert cost("claude-sonnet-5-5", 12_345, 678) == Decimal("0.03147")
    # Haiku 5.5: $0.10 in, $0.50 out.
    assert cost("claude-haiku-5-5", 1_000_000, 1_000_000) == Decimal("0.60")
    assert cost("claude-fable-5-2", 0, 0) == Decimal("0")


def test_cost_refuses_an_unpriced_model():
    with pytest.raises(ConfigError, match="claude-unknown"):
        cost("claude-unknown", 1, 1)


def test_default_models_are_all_before_the_default_window():
    config = make_config()
    cutoffs = assert_window_after_cutoffs(config, config.evaluation.window_start)
    assert cutoffs["claude-sonnet-5-5"] == date(2026, 6, 30)


def test_a_window_starting_on_a_cutoff_is_refused():
    config = make_config()
    with pytest.raises(ConfigError, match="knowledge cutoff"):
        assert_window_after_cutoffs(config, date(2026, 6, 30))
    with pytest.raises(ConfigError):
        assert_window_after_cutoffs(config, date(2026, 6, 1))
