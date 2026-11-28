"""The shared contracts: records, configuration, and the output schemas roles must satisfy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmfa.config import Config, load_config
from pmfa.errors import ConfigError
from pmfa.records.base import stable_id
from pmfa.records.market import BookLevel, OrderBook, Side
from pmfa.records.roles import TASKS, ModelSlot, Quantiles


def _schema_problems(node: object, path: str = "") -> list[str]:
    problems: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            problems.append(f"{path}: object is not closed")
        for keyword in ("minimum", "maximum", "minLength", "maxLength", "pattern", "multipleOf"):
            if keyword in node:
                problems.append(f"{path}: uses {keyword}")
        for key, value in node.items():
            problems += _schema_problems(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            problems += _schema_problems(value, f"{path}[{i}]")
    return problems


@pytest.mark.parametrize("task", list(TASKS))
def test_role_output_schemas_fit_native_structured_output(task):
    assert _schema_problems(TASKS[task].output_type.model_json_schema()) == []


def test_defaults_are_valid_and_every_slot_has_a_priced_model():
    config = Config()
    for slot in ModelSlot:
        assert config.models.slots[slot].model in config.models.catalog
    assert config.full_tree_calls() <= config.budget.max_calls_per_episode


def test_per_order_cap_must_stay_below_the_exchange_breaker():
    with pytest.raises(ConfigError, match="order_group_contracts_limit"):
        load_config(overrides={"caps": {"max_contracts_per_order": 50}})


def test_the_demo_subaccount_is_the_main_balance_or_a_numbered_one():
    assert load_config(overrides={"kalshi": {"demo_subaccount": 0}}).kalshi.demo_subaccount == 0
    for bad in (-1, 64):
        with pytest.raises(ConfigError, match="demo_subaccount"):
            load_config(overrides={"kalshi": {"demo_subaccount": bad}})


def test_unknown_configuration_keys_are_rejected():
    with pytest.raises(ConfigError):
        load_config(overrides={"caps": {"per_market_fracton": "0.05"}})


def test_buying_one_side_matches_the_other_sides_bids():
    book = OrderBook(
        ticker="T",
        as_of=datetime(2026, 11, 24, tzinfo=UTC),
        yes_bids=[BookLevel(price="0.40", contracts="100")],
        no_bids=[
            BookLevel(price="0.58", contracts="300"),
            BookLevel(price="0.57", contracts="900"),
        ],
    )
    best = book.best_ask(Side.YES)
    assert best is not None and best.price == Decimal("0.4200") and best.contracts == Decimal("300")
    assert book.best_ask(Side.NO).price == Decimal("0.6000")
    assert (
        book.resting_dollars_near_touch(Side.YES, Decimal("0.02"))
        == Decimal("0.42") * 300 + Decimal("0.43") * 900
    )


def test_quantiles_must_not_decrease():
    with pytest.raises(ValueError):
        Quantiles(q05=3.0, q25=2.9, q50=3.1, q75=3.2, q95=3.4)


def test_stable_ids_are_deterministic():
    assert stable_id("ep", "a", 1) == stable_id("ep", "a", 1) != stable_id("ep", "a", 2)


def test_an_empty_config_variable_means_not_set(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PMFA_CONFIG", "")
    assert load_config() == Config()


def test_an_unknown_order_outcome_is_a_status_of_its_own_and_not_an_exit():
    from pmfa.records.decision import ProposalStatus
    from pmfa.records.episode import EpisodeStatus, Exit

    assert EpisodeStatus("outcome_unknown") and ProposalStatus("outcome_unknown")
    assert "outcome_unknown" not in {e.value for e in Exit}
