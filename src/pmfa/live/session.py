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

from pmfa.errors import PmfaError
from pmfa.records.base import RunMode
from pmfa.world.live import build_live_world, live_paths

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.episode import EpisodeController
    from pmfa.ports import Models, World
    from pmfa.records.decision import ReasonCode, ReviewDecision
    from pmfa.records.episode import EpisodeRecord
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


class NotDecided(PmfaError):
    """A review that cannot be applied; the message says why in plain words."""


def decide(
    s: Session,
    proposal_id: str,
    decision: ReviewDecision,
    *,
    contracts: int | None = None,
    reason_code: ReasonCode | None = None,
    note: str = "",
) -> EpisodeRecord:
    """Apply a person's decision on a pending proposal and resume its episode to its exit.

    ``pmfa review approve|resize|reject`` and ``pmfa ask`` both decide through
    this. It refuses (``NotDecided``) a proposal that does not exist, whose order
    outcome is unknown, whose episode has ended, or whose episode stopped
    unfinished; anything else goes to ``EpisodeController.resume``, which records
    the review and re-runs the gate at the current price before any order.
    """
    from pmfa.episode import NotPaused
    from pmfa.errors import NotFound
    from pmfa.execution.review import ReviewRefused
    from pmfa.live.text import unknown_outcome_sentence
    from pmfa.records.decision import ProposalStatus, Review
    from pmfa.records.episode import EpisodeStatus

    mode = s.world.mode
    found = s.world.store.get_proposal(proposal_id)
    if found is None:
        raise NotDecided(f"no proposal {proposal_id!r} ({mode.value})")
    proposal = found[0]
    if found[1] is ProposalStatus.OUTCOME_UNKNOWN:
        raise NotDecided(
            f"not decided: the order for {proposal_id} was sent and its outcome is "
            "unknown, so a review cannot change it. " + unknown_outcome_sentence(mode)
        )
    episode = s.world.store.get_episode(proposal.episode_id)
    if episode is not None and episode.status is EpisodeStatus.DONE:
        raise NotDecided(
            f"not decided: {proposal_id} is {found[1].value}, not pending: its episode has ended"
        )
    if episode is not None and s.controller.in_doubt(proposal.episode_id):
        # Only an approved or resized proposal can have had an order sent.
        sent = found[1] in (ProposalStatus.APPROVED, ProposalStatus.RESIZED)
        raise NotDecided(
            f"not decided: {proposal_id} is already {found[1].value}, and "
            + ("its order may have been sent" if sent else "its episode stopped unfinished")
            + f". If no process is still running it, pmfa reconcile --mode {mode.value} "
            f"--episode {proposal.episode_id} {'settles' if sent else 'finishes'} it."
        )
    review = Review(
        proposal_id=proposal_id,
        decision=decision,
        contracts=contracts,
        reason_code=reason_code,
        note=note,
        decided_at=s.world.clock.now(),
    )
    try:
        return s.controller.resume(proposal.episode_id, review)
    except (ReviewRefused, NotPaused, NotFound, ValueError) as exc:
        raise NotDecided(f"not decided: {exc}") from None


__all__ = [
    "LiveMode",
    "NotDecided",
    "Session",
    "build_world",
    "close_world",
    "decide",
    "open_session",
    "open_store",
]
