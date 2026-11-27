"""The shared contracts: records, configuration, and the output schemas roles must satisfy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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


EXAMPLE_WORLD = Path(__file__).resolve().parents[1] / "examples/episodes/cpi-ladder/world.json"

REMOVED = [
    # Removed: the scoring horizons are evaluation.horizons_days.
    ("triggers", "schedule_horizons_days", [14, 7], r"evaluation\.horizons_days"),
    # Removed: the Quantiles record fixes the five levels.
    ("forecast", "quantile_levels", [0.1, 0.2, 0.5, 0.8, 0.9], r"Quantiles record"),
]
OLD_DEFAULTS = {
    "schedule_horizons_days": [7, 3, 1],
    "quantile_levels": [0.05, 0.25, 0.5, 0.75, 0.95],
}


@pytest.mark.parametrize(("section", "key", "value", "where"), REMOVED)
def test_removed_configuration_keys_are_refused_not_ignored(section, key, value, where):
    for given in (value, OLD_DEFAULTS[key]):
        with pytest.raises(ConfigError, match=rf"{section}\.{key}\s+no longer a setting.*{where}"):
            load_config(overrides={section: {key: given}})


def test_a_config_file_that_still_sets_a_removed_key_is_refused(tmp_path):
    file = tmp_path / "old.toml"
    file.write_text("[triggers]\nschedule_horizons_days = [7, 3, 1]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"evaluation\.horizons_days"):
        load_config(file)


def test_a_removed_key_does_not_hide_the_other_problems():
    overrides = {"forecast": {"quantile_levels": [0.5]}, "caps": {"per_market_fraction": "2"}}
    with pytest.raises(ConfigError) as caught:
        load_config(overrides=overrides)
    assert "forecast.quantile_levels" in str(caught.value)
    assert "caps.per_market_fraction must be a fraction" in str(caught.value)


def test_a_recorded_configuration_with_a_removed_key_still_reads():
    # A published run's manifest records the configuration of its day, removed keys included,
    # at their old defaults: those meant nothing, so the record reads back as it was.
    recorded = Config().model_dump(mode="json")
    recorded["triggers"]["schedule_horizons_days"] = [7, 3, 1]
    recorded["forecast"]["quantile_levels"] = [0.05, 0.25, 0.5, 0.75, 0.95]
    assert Config.model_validate(recorded) == Config()


@pytest.mark.parametrize(("section", "key", "value", "where"), REMOVED)
def test_a_recorded_world_that_changes_a_removed_key_is_refused(section, key, value, where):
    # Recorded worlds' config_overrides are partly hand-written; a changed value is an intent
    # the code would ignore, so it is refused there too.
    from pmfa.world.recorded import load_recorded_world, recorded_config

    world = load_recorded_world(EXAMPLE_WORLD)
    edited = world.model_copy(update={"config_overrides": {section: {key: value}}})
    with pytest.raises(ConfigError, match=rf"{section}\.{key}\s+no longer a setting.*{where}"):
        recorded_config(edited)


def test_config_check_says_where_a_removed_setting_went(tmp_path):
    from typer.testing import CliRunner

    from pmfa.cli.app import app

    file = tmp_path / "old.toml"
    file.write_text(
        "[triggers]\nschedule_horizons_days = [14]\n\n[forecast]\nquantile_levels = [0.5]\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["config", "check", str(file)])
    assert result.exit_code == 1
    assert "2 problem(s)" in result.output
    assert "triggers.schedule_horizons_days: no longer a setting" in result.output
    assert "evaluation.horizons_days" in result.output
    assert "forecast.quantile_levels: no longer a setting" in result.output


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
