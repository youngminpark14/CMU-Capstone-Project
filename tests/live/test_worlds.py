"""The live world builders: shadow cannot trade and reads no credential; demo needs its key and host.

Nothing here touches the network: building a world opens files and HTTP clients
but sends no request.
"""

from __future__ import annotations

import os
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pmfa.config import load_config
from pmfa.errors import ConfigError
from pmfa.execution.paper import PaperExecutor
from pmfa.models import Gateway, ScriptedModels
from pmfa.records.base import RunMode
from pmfa.records.decision import ReviewPolicy
from pmfa.world.kalshi.demo import DemoExecutor, is_demo_url
from pmfa.world.kalshi.production import ProductionExchange
from pmfa.world.live import (
    TRACING_VARIABLES,
    build_demo_world,
    build_shadow_world,
    live_paths,
)

KALSHI_VARIABLES = ("KALSHI_DEMO_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY", "KALSHI_DEMO_PRIVATE_KEY_PATH")
ORDER_METHODS = ("place", "cancel_all", "balance", "positions", "create_order", "cancel_order")


class TrackingEnviron(dict):
    """An environment that remembers which variables were read."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        self.read.add(key)
        return super().get(key, default)

    def __contains__(self, key):  # type: ignore[no-untyped-def]
        self.read.add(key)
        return super().__contains__(key)


@pytest.fixture
def config(tmp_path):
    return load_config(overrides={"paths": {"data_dir": str(tmp_path / "data")}})


def _pem() -> str:
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_the_shadow_world_reads_no_kalshi_credential_and_its_exchange_cannot_trade(
    config, monkeypatch
):
    env = TrackingEnviron(os.environ)
    for name in KALSHI_VARIABLES:
        env[name] = "should-never-be-read"
    env["LANGSMITH_TRACING"] = "true"
    monkeypatch.setattr(os, "environ", env)

    world = build_shadow_world(config, models=ScriptedModels({}, config=config))
    try:
        assert not env.read & set(KALSHI_VARIABLES)
        assert all(env[name] == "false" for name in TRACING_VARIABLES)
        assert world.mode is RunMode.SHADOW
        assert isinstance(world.exchange, ProductionExchange)
        assert world.exchange.client.signer is None
        assert not any(hasattr(world.exchange, name) for name in ORDER_METHODS)
        assert isinstance(world.executor, PaperExecutor) and world.executor.simulated
        assert world.review_policy is ReviewPolicy.FILL_TAGGED
        assert world.state_db == live_paths(config, RunMode.SHADOW)[0] == config.paths.state_db()
        assert world.state_db.exists()
    finally:
        world.close()


def test_shadow_with_review_pauses_and_the_default_models_are_the_live_gateway(config):
    world = build_shadow_world(config, review=True)
    try:
        assert world.review_policy is ReviewPolicy.PAUSE
        assert isinstance(world.models, Gateway)
        assert world.models.meter.enforce_seconds
        assert world.models.recorder is not None
        assert str(world.models.recorder.directory).startswith(config.paths.data_dir)
    finally:
        world.close()


def test_the_demo_world_refuses_to_build_without_credentials_and_names_the_variables(
    config, monkeypatch
):
    for name in KALSHI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as caught:
        build_demo_world(config, models=ScriptedModels({}, config=config))
    message = str(caught.value)
    # Each variable is named exactly once (the message does not repeat itself).
    assert all(len(re.findall(rf"\b{name}\b", message)) == 1 for name in KALSHI_VARIABLES)
    assert not live_paths(config, RunMode.DEMO)[0].exists()  # nothing was opened


def test_the_demo_world_refuses_a_non_demo_host(config, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_KEY_ID", "key-id")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", _pem())
    production = config.model_copy(
        update={
            "kalshi": config.kalshi.model_copy(
                update={"demo_base_url": config.kalshi.production_base_url}
            )
        }
    )
    with pytest.raises(ConfigError, match="demo host"):
        build_demo_world(production, models=ScriptedModels({}, config=config))


def test_a_partial_credential_is_refused_without_showing_the_key(config, monkeypatch):
    pem = _pem()
    monkeypatch.delenv("KALSHI_DEMO_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", pem)
    with pytest.raises(ConfigError) as caught:
        build_demo_world(config, models=ScriptedModels({}, config=config))
    assert "KALSHI_DEMO_KEY_ID is not set but a Kalshi private key is" in str(caught.value)
    assert "PRIVATE KEY" not in str(caught.value)


def test_the_demo_world_trades_only_on_the_demo_host_and_always_pauses(config, monkeypatch):
    monkeypatch.setenv("KALSHI_DEMO_KEY_ID", "key-id")
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", _pem())
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    world = build_demo_world(config, models=ScriptedModels({}, config=config))
    try:
        assert world.mode is RunMode.DEMO
        assert world.review_policy is ReviewPolicy.PAUSE
        assert isinstance(world.executor, DemoExecutor) and not world.executor.simulated
        assert is_demo_url(world.executor.client.base_url)
        assert is_demo_url(world.exchange.client.base_url)
        assert repr(world.executor.client.signer) == "KalshiSigner(algorithm='ed25519')"
        assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
        # Demo keeps its own state files, apart from shadow's paper account.
        assert world.state_db != config.paths.state_db()
        assert world.checkpoint_db != config.paths.checkpoint_db()
    finally:
        world.close()
