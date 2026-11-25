"""Exchange trading fees.

The exchange charges a quadratic fee on the expected earnings of a trade:
``rate x multiplier x contracts x price x (1 - price)``, rounded up to the next
cent. The taker rate is 0.07. The maker rate depends on the series' fee type.
Fee type and multiplier always come from the ``Series`` record, so a schedule
change on the exchange is a data change here, never a code change.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from pmfa.errors import PmfaError
from pmfa.records.base import CENT, ONE, ZERO
from pmfa.records.market import Series

TAKER_RATE = Decimal("0.07")
"""The General Trading Fees rate for orders that take liquidity."""

# Maker rates per fee type. The OpenAPI document describes the types:
# ``quadratic`` is the general table with no maker fee;
# ``quadratic_with_maker_fees`` adds maker fees at a quarter of the taker rate (0.0175);
# ``quadratic_with_combo_maker_fees`` is the same structure with a 0.5 maker multiplier.
_MAKER_RATES: dict[str, Decimal] = {
    "quadratic": ZERO,
    "quadratic_with_maker_fees": Decimal("0.0175"),
    "quadratic_with_combo_maker_fees": TAKER_RATE * Decimal("0.5"),
}

KNOWN_FEE_TYPES = frozenset(_MAKER_RATES)
"""Fee types this module can price. ``flat`` (the Specific Trading Fees Table) is not one."""


class UnknownFeeType(PmfaError, ValueError):
    """The series uses a fee type whose formula this module does not know.

    Pricing it with a guess would misstate every edge, so sizing refuses instead.
    """


def fee_rate(fee_type: str, *, taker: bool = True) -> Decimal:
    """The quadratic rate for ``fee_type``: 0.07 for takers, the type's maker rate otherwise."""
    if fee_type not in _MAKER_RATES:
        raise UnknownFeeType(
            f"unknown fee type {fee_type!r}; known: {', '.join(sorted(KNOWN_FEE_TYPES))}"
        )
    return TAKER_RATE if taker else _MAKER_RATES[fee_type]


def _check_inputs(contracts: int | Decimal, price: Decimal, multiplier: Decimal) -> None:
    for name, value in (("contracts", contracts), ("price", price), ("multiplier", multiplier)):
        if isinstance(value, float):
            raise TypeError(f"{name} must be a Decimal or int, not float")
    if contracts < 0:
        raise ValueError(f"contracts cannot be negative, got {contracts}")
    if not (ZERO <= price <= ONE):
        raise ValueError(f"price {price} is outside [0, 1]")
    if multiplier < 0:
        raise ValueError(f"fee multiplier cannot be negative, got {multiplier}")


def exact_fee(
    contracts: int | Decimal,
    price: Decimal,
    fee_type: str,
    multiplier: Decimal,
    *,
    taker: bool = True,
) -> Decimal:
    """The model fee before rounding, in dollars."""
    _check_inputs(contracts, price, multiplier)
    rate = fee_rate(fee_type, taker=taker)
    return rate * Decimal(multiplier) * Decimal(contracts) * price * (ONE - price)


def round_up_cent(amount: Decimal) -> Decimal:
    """Round a dollar amount up to the next cent."""
    return amount.quantize(CENT, rounding=ROUND_CEILING)


def fee(
    contracts: int | Decimal,
    price: Decimal,
    fee_type: str,
    multiplier: Decimal,
    taker: bool = True,
) -> Decimal:
    """The fee for one order of ``contracts`` at ``price``, rounded up to the cent.

    Raises ``UnknownFeeType`` for a fee type this module cannot price.
    """
    return round_up_cent(exact_fee(contracts, price, fee_type, multiplier, taker=taker))


def per_contract_fee(
    price: Decimal, fee_type: str, multiplier: Decimal, *, taker: bool = True
) -> Decimal:
    """The unrounded fee for one contract at ``price``: the marginal fee used in edge math."""
    return exact_fee(1, price, fee_type, multiplier, taker=taker)


def series_fee(
    contracts: int | Decimal, price: Decimal, series: Series, *, taker: bool = True
) -> Decimal:
    """``fee`` with the fee type and multiplier read from ``series``."""
    return fee(contracts, price, series.fee_type, series.fee_multiplier, taker)
