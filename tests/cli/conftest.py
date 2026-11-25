"""Fixtures for the command tests: a data directory, a config file naming it, and a patched world.

``live`` points ``$PMFA_CONFIG`` at a file whose ``paths.data_dir`` is under
``tmp_path`` and replaces ``pmfa.live.session.build_world`` with the small
recorded world of ``tests.live.support``: its state database and checkpoints
are files in that directory, so two ``CliRunner`` invocations share them the way
two processes would. ``live.start`` sets the clock of the next world built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from pmfa.live import loop as loop_module
from pmfa.live import session as session_module
from pmfa.records.base import RunMode
from pmfa.records.decision import ReviewPolicy
from tests.live.support import AS_OF, BEFORE, live_file, live_world, no_resolver


@dataclass
class Live:
    data_dir: Path
    config_file: Path
    start: datetime = AS_OF
    built: list[RunMode] = field(default_factory=list)

    def last_look(self, mode: RunMode = RunMode.SHADOW, at: datetime = BEFORE) -> None:
        """Pretend an earlier pass looked at ``at``, so documents after it are new."""
        loop_module.write_last_look(self.data_dir / f"live-{mode.value}-cursor.json", at)


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Live:
    data_dir = tmp_path / "data"
    config_file = tmp_path / "pmfa.toml"
    config_file.write_text(f'[paths]\ndata_dir = "{data_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("PMFA_CONFIG", str(config_file))
    state = Live(data_dir=data_dir, config_file=config_file)

    def fake_build_world(mode, config, *, review=False, models=None):  # type: ignore[no-untyped-def]
        mode = RunMode(mode)
        state.built.append(mode)
        pause = review or mode is RunMode.DEMO
        return live_world(
            live_file(data_dir),
            policy=ReviewPolicy.PAUSE if pause else ReviewPolicy.FILL_TAGGED,
            mode=mode,
            start=state.start,
        )

    monkeypatch.setattr(session_module, "build_world", fake_build_world)
    monkeypatch.setattr(loop_module, "default_resolver", lambda: no_resolver)
    return state
