"""Fixtures for the snapshot tests: a store built from the invented exchange."""

from __future__ import annotations

from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.world.snapshot.build import build_snapshot
from pmfa.world.snapshot.store import SnapshotStore
from tests.world.snapshot.fakes import BUILT, FakeExchange


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path: Path, config: Config) -> SnapshotStore:
    """A snapshot of the invented exchange, built for the default window."""
    snapshot = SnapshotStore(tmp_path / "snapshot.sqlite")
    build_snapshot(config, FakeExchange(), snapshot, end=BUILT)
    yield snapshot
    snapshot.close()
