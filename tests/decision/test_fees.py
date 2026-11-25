"""Fees: the quadratic model, its rounding, and the committed examples."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmfa.decision import KNOWN_FEE_TYPES, UnknownFeeType, exact_fee, fee, fee_rate, series_fee
from tests.decision.factories import PROPERTY, series

EXAMPLES = json.loads((Path(__file__).parent / "fee_schedule_examples.json").read_text())


def test_examples_file_says_where_its_numbers_come_from():
    assert EXAMPLES["source_url"] == "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
    assert EXAMPLES["retrieved_on"] == "2026-11-25"
    assert EXAMPLES["retrieval_status"] in {"retrieved", "not_retrieved"}
    for example in EXAMPLES["examples"]:
        assert example["label"] in {"schedule", "hand-computed", "exchange-docs"}


@pytest.mark.parametrize(
    "example", EXAMPLES["examples"], ids=lambda e: f"{e['contracts']}@{e['price']}-{e['fee_type']}"
)
def test_fee_reproduces_every_committed_example(example):
    args = (
        example["contracts"],
        Decimal(example["price"]),
        example["fee_type"],
        Decimal(example["multiplier"]),
    )
    assert exact_fee(*args, taker=example["taker"]) == Decimal(example["exact_fee"])
    assert fee(*args, taker=example["taker"]) == Decimal(example["fee"])


def test_fee_type_and_multiplier_come_from_the_series():
    assert series_fee(100, Decimal("0.5"), series(multiplier="2")) == Decimal("3.50")
    assert series_fee(100, Decimal("0.5"), series(fee_type="quadratic"), taker=False) == 0


@pytest.mark.parametrize("fee_type", ["flat", "linear", ""])
def test_unknown_fee_types_raise(fee_type):
    with pytest.raises(UnknownFeeType):
        fee(10, Decimal("0.5"), fee_type, Decimal("1"))
    with pytest.raises(UnknownFeeType):
        fee_rate(fee_type)


def test_money_is_never_a_float():
    with pytest.raises(TypeError):
        fee(10, 0.5, "quadratic", Decimal("1"))  # type: ignore[arg-type]


@pytest.mark.parametrize("price", ["-0.01", "1.01"])
def test_prices_outside_zero_one_are_refused(price):
    with pytest.raises(ValueError):
        fee(10, Decimal(price), "quadratic", Decimal("1"))


cents = st.integers(min_value=0, max_value=10000).map(lambda n: Decimal(n) / 10000)


@PROPERTY
@given(
    contracts=st.integers(min_value=0, max_value=10000),
    price=cents,
    fee_type=st.sampled_from(sorted(KNOWN_FEE_TYPES)),
    multiplier=st.sampled_from([Decimal("0.5"), Decimal("1"), Decimal("2")]),
    taker=st.booleans(),
)
def test_fee_rounds_the_model_fee_up_to_the_next_cent(
    contracts, price, fee_type, multiplier, taker
):
    exact = exact_fee(contracts, price, fee_type, multiplier, taker=taker)
    rounded = fee(contracts, price, fee_type, multiplier, taker)
    assert rounded == rounded.quantize(Decimal("0.01"))
    assert exact <= rounded < exact + Decimal("0.01")
    assert fee(contracts + 1, price, fee_type, multiplier, taker) >= rounded
    if taker:
        assert exact == Decimal("0.07") * multiplier * contracts * price * (1 - price)
