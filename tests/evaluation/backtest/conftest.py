"""One full backtest over the tiny frozen world, shared by the tests that only read its outputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.evaluation.manifest import RunManifest
from pmfa.models import ScriptedModels
from tests.evaluation.backtest.support import ALL_SYSTEMS, build_data, make_config, scripted


@dataclass
class FullRun:
    config: Config
    run_dir: Path
    manifest: RunManifest
    models: ScriptedModels


@pytest.fixture(scope="module")
def full_run(tmp_path_factory: pytest.TempPathFactory) -> FullRun:
    from pmfa.evaluation.backtest import run_backtest

    root = tmp_path_factory.mktemp("backtest")
    config = make_config(root / "data")
    build_data(config)
    models = scripted(config)
    run_dir = root / "runs" / "full"
    manifest = run_backtest(config, run_dir=run_dir, systems=ALL_SYSTEMS, models=models)
    return FullRun(config, run_dir, manifest, models)


@pytest.fixture
def data_config(tmp_path: Path) -> Config:
    """A fresh frozen world for a test that runs its own backtest."""
    config = make_config(tmp_path / "data")
    build_data(config)
    return config
