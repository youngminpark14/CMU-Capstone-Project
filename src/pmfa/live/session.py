"""A live session: the world of a mode, and a controller over its store and checkpoint files.

Every operator command builds its world through ``build_world`` here, so a
review decided in one process resumes the episode another process paused: both
open the same state database and the same SQLite checkpointer (``live_paths``).
Tests replace ``build_world`` with a small recorded world.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pmfa.records.base import RunMode
from pmfa.world.live import build_live_world, live_paths

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.episode import EpisodeController
    from pmfa.ports import Models, World
    from pmfa.state.store import SqliteStore


class LiveMode(StrEnum):
    """The modes an operator command can act on."""

    SHADOW = "shadow"
    DEMO = "demo"

    @property
    def run_mode(self) -> RunMode:
        return RunMode(self.value)


def build_world(
    mode: RunMode, config: Config, *, review: bool = False, models: Models | None = None
) -> World:
    """The world of a live mode (the seam tests replace)."""
    return build_live_world(mode, config, models=models, review=review)


def close_world(world: World) -> None:
    """Close what a world holds open: its own ``close``, else its store and index."""
    close = getattr(world, "close", None)
    if callable(close):
        close()
        return
    for port in (world.store, world.knowledge):
        port_close = getattr(port, "close", None)
        if callable(port_close):
            with contextlib.suppress(Exception):
                port_close()


@dataclass
class Session:
    world: World
    controller: EpisodeController
    state_db: Path
    checkpoint_db: Path


@contextlib.contextmanager
def open_session(
    mode: RunMode, config: Config, *, review: bool = False, models: Models | None = None
) -> Iterator[Session]:
    """Build the world and a controller with the mode's SQLite checkpointer; close both after."""
    from pmfa.episode import EpisodeController, open_checkpointer

    mode = RunMode(mode)
    state_db, checkpoint_db = live_paths(config, mode)
    world = build_world(mode, config, review=review, models=models)
    try:
        saver = open_checkpointer(checkpoint_db)
        try:
            controller = EpisodeController(world, checkpointer=saver)
            try:
                yield Session(world, controller, state_db, checkpoint_db)
            finally:
                controller.close()
        finally:
            saver.conn.close()
    finally:
        close_world(world)


@contextlib.contextmanager
def open_store(mode: RunMode, config: Config) -> Iterator[SqliteStore]:
    """Only the mode's state database: for commands that need no exchange, evidence or models."""
    from pmfa.state.store import SqliteStore

    state_db, _ = live_paths(config, mode)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(state_db)
    try:
        yield store
    finally:
        store.close()


__all__ = ["LiveMode", "Session", "build_world", "close_world", "open_session", "open_store"]
