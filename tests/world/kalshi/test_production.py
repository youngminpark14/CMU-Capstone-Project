"""The production adapter is read-only by construction: no order methods, no signer, no credentials."""

from __future__ import annotations

import inspect

import pytest

import pmfa.world.kalshi.production as production_module
import pmfa.world.kalshi.signing as signing_module
from pmfa.errors import ConfigError
from pmfa.ports import Exchange, Executor
from pmfa.world.kalshi.production import ProductionExchange, production_exchange
from pmfa.world.kalshi.signing import KalshiSigner
from tests.world.kalshi.fakes import FakeKalshi, Sleeps, client, clock, config

FORBIDDEN = {
    "place",
    "cancel",
    "cancel_all",
    "cancel_order",
    "create_order",
    "amend",
    "decrease",
    "balance",
    "positions",
    "execution_book",
    "fills",
    "orders",
    "order_group",
}


def test_the_class_has_no_order_cancel_or_portfolio_methods() -> None:
    names = {n for n, _ in inspect.getmembers(ProductionExchange) if not n.startswith("__")}
    assert not names & FORBIDDEN
    assert not any("order" in n and n != "get_order_book" for n in names if not n.startswith("_"))
    assert not any("portfolio" in n for n in names)
    exchange = production_exchange(config(), clock())
    assert isinstance(exchange, Exchange)
    assert not isinstance(exchange, Executor)


def test_it_is_built_without_a_signer_and_refuses_one() -> None:
    exchange = production_exchange(config(), clock())
    assert exchange.client.signer is None
    assert exchange.client.host == "external-api.kalshi.com"
    signed = client(FakeKalshi(), signer=object())
    with pytest.raises(ConfigError):
        ProductionExchange(signed, clock(), config())


def test_it_never_reads_the_credential_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config()
    for name in (
        cfg.kalshi.demo_key_id_env,
        cfg.kalshi.demo_private_key_env,
        cfg.kalshi.demo_private_key_path_env,
    ):
        monkeypatch.setenv(name, "must-not-be-read")

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the production adapter loaded a signer")

    monkeypatch.setattr(signing_module, "load_signer", refuse)
    monkeypatch.setattr(signing_module.KalshiSigner, "__init__", refuse)
    assert "signing" not in production_module.__dict__
    assert KalshiSigner.__name__ not in production_module.__dict__

    fake = FakeKalshi().add("GET", "/series/KXFEDDECISION", "series_KXFEDDECISION")
    sleeps = Sleeps()
    exchange = production_exchange(
        cfg, clock(), transport=fake.transport(), sleep=sleeps.sleep, monotonic=sleeps.monotonic
    )
    assert exchange.get_series("KXFEDDECISION").ticker == "KXFEDDECISION"
    (request,) = fake.requests
    assert not any(h.lower().startswith("kalshi-access") for h in request.headers)
    assert request.url.host == "external-api.kalshi.com"
