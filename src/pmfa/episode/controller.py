"""The episode controller: run an episode for a trigger and return its record.

The same code runs in every mode; only the world behind the ports differs.
``run`` goes to an exit, or to the review pause (status ``PENDING_REVIEW``,
with the proposal and its card stored). ``resume`` applies a human review and
``expire`` expires the pending proposal; both continue the paused thread from
its checkpoint, which may have been written by another controller object (or
process) over the same store and checkpoint files.

A thread that stopped at the ``execute`` node (the process died, or an
exception escaped, between the approval and the order being recorded) is
continued at that node by ``run``, ``resume`` and ``expire`` alike. ``execute``
looks the order up by its deterministic client order id before sending
anything, so the continuation records an order that was already sent instead
of sending a second one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pmfa.errors import NotFound, PmfaError, RunBudgetExhausted
from pmfa.ports import World
from pmfa.records.decision import ProposalStatus, Review
from pmfa.records.episode import EpisodeRecord, EpisodeStatus
from pmfa.records.evidence import Trigger
from pmfa.state.client import StateClient
from pmfa.state.service import StateService

from .graph import RECURSION_LIMIT, build_graph
from .pipeline import episode_id_for
from .trace import Tracer, dump

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.sqlite import SqliteSaver


class NotPaused(PmfaError):
    """``resume`` or ``expire`` was called for an episode whose graph is not waiting for review."""


def open_checkpointer(path: str | Path) -> SqliteSaver:
    """A SQLite checkpointer for the live modes, where one process pauses and another resumes.

    The caller owns it: close it with ``saver.conn.close()`` when done.
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(target), check_same_thread=False))


def state_client(world: World) -> StateClient:
    """The roles' shared state, through the in-process MCP server or the service directly."""
    service = StateService(world.store, world.knowledge, world.exchange)
    if world.config.state.transport == "direct":
        return StateClient.direct(service)
    return StateClient.mcp(service)


class EpisodeController:
    """Runs episodes over one world. Close it when done (it holds the state client)."""

    def __init__(self, world: World, *, checkpointer: BaseCheckpointSaver[Any] | None = None):
        if checkpointer is None:
            from langgraph.checkpoint.memory import InMemorySaver

            checkpointer = InMemorySaver()
        self.world = world
        self.checkpointer = checkpointer
        self.client = state_client(world)
        self.tracer = Tracer(world.store, world.clock)
        self.graph = build_graph(
            world, client=self.client, checkpointer=checkpointer, tracer=self.tracer
        )

    # ------------------------------------------------------------------ public

    def run(self, trigger: Trigger) -> EpisodeRecord:
        """Run the episode for ``trigger`` to an exit or to the review pause.

        The episode id is deterministic, so running a trigger that already has an
        episode in the store returns that episode without running anything again,
        unless its thread stopped at ``execute``: that is continued there. Raises
        ``RunBudgetExhausted`` after recording the episode (exit ``error``) when the
        run's dollar cap was reached: the caller must stop the run.
        """
        episode_id = episode_id_for(self.world.mode, trigger)
        existing = self.world.store.get_episode(episode_id)
        if existing is not None:
            if existing.status is not EpisodeStatus.DONE and self._stopped_at_execute(episode_id):
                return self._invoke(episode_id, None)
            return existing
        initial = {
            "episode_id": episode_id,
            "mode": self.world.mode.value,
            "trigger": dump(trigger),
            "as_of": trigger.at.isoformat(),
            "started_at": self.world.clock.now().isoformat(),
            "status": EpisodeStatus.RUNNING.value,
        }
        return self._invoke(episode_id, initial)

    def resume(self, episode_id: str, review: Review) -> EpisodeRecord:
        """Apply a human review to a paused episode and run it to its exit.

        The review is validated and recorded by the review queue first: an invalid
        one (wrong proposal, a resize that does not shrink, a reject without a
        reason code) raises and leaves the episode paused. A proposal that has
        expired, by time or because the price passed its edge bound, ends the
        episode ``expired`` whatever the review says. Resuming an episode that is
        no longer paused returns its record and does nothing. An episode whose
        review was applied but whose thread stopped at ``execute`` is continued
        there; the review given now is not applied again. Raises
        ``RunBudgetExhausted`` after recording, as ``run`` does.
        """
        from pmfa.execution.review import ReviewQueue

        stored = self._stored(episode_id)
        if stored.status is EpisodeStatus.PENDING_REVIEW and self._stopped_at_execute(episode_id):
            return self._invoke(episode_id, None)
        stored, proposal = self._paused(episode_id)
        if stored.status is not EpisodeStatus.PENDING_REVIEW:
            return stored
        if review.proposal_id != proposal.proposal_id:
            raise ValueError(
                f"the review is for {review.proposal_id}, the episode waits on "
                f"{proposal.proposal_id}"
            )
        queue = ReviewQueue(self.world.store, self.world.clock, self.world.config)
        _, status, _ = queue.get(proposal.proposal_id)
        answer: dict[str, Any]
        if status is ProposalStatus.PENDING:
            book = self.world.executor.execution_book(proposal.market_ticker)
            if queue.expired(proposal, book):
                queue.expire(proposal.proposal_id)
                answer = self._expired_answer(proposal, book)
            else:
                recorded = queue.decide(
                    proposal.proposal_id,
                    review.decision,
                    contracts=review.contracts,
                    reason_code=review.reason_code,
                    note=review.note,
                    book=book,
                    reviewer=review.reviewer,
                )
                answer = {"kind": "review", "review": dump(recorded)}
        elif status is ProposalStatus.EXPIRED:
            answer = {"kind": "expired", "detail": "the proposal expired before the review"}
        else:  # decided earlier, but the graph never resumed: use the recorded decision
            recorded_reviews = [
                r for r in self.world.store.list_reviews() if r.proposal_id == proposal.proposal_id
            ]
            if not recorded_reviews:
                raise NotPaused(f"proposal {proposal.proposal_id} is {status.value}")
            answer = {"kind": "review", "review": dump(recorded_reviews[-1])}
        return self._resume(episode_id, answer)

    def expire(self, episode_id: str) -> EpisodeRecord:
        """Expire a paused episode's proposal: it ends ``expired`` and never executes.

        An approved episode whose thread stopped at ``execute`` may already have sent its
        order, so it is continued there instead (``execute`` itself expires a proposal
        whose order was never sent once ``expires_at`` has passed).
        """
        from pmfa.execution.review import ReviewQueue

        stored = self._stored(episode_id)
        if stored.status is EpisodeStatus.PENDING_REVIEW and self._stopped_at_execute(episode_id):
            return self._invoke(episode_id, None)
        stored, proposal = self._paused(episode_id)
        if stored.status is not EpisodeStatus.PENDING_REVIEW:
            return stored
        queue = ReviewQueue(self.world.store, self.world.clock, self.world.config)
        _, status, _ = queue.get(proposal.proposal_id)
        if status is ProposalStatus.PENDING:
            queue.expire(proposal.proposal_id)
        return self._resume(
            episode_id, {"kind": "expired", "detail": "the proposal was expired before review"}
        )

    def close(self) -> None:
        """Close the state client (and with it the in-process MCP session)."""
        self.client.close()

    def __enter__(self) -> EpisodeController:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ internals

    def _config(self, episode_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": episode_id}, "recursion_limit": RECURSION_LIMIT}

    def _invoke(self, episode_id: str, value: Any) -> EpisodeRecord:
        result = self.graph.invoke(value, self._config(episode_id))
        record = self.world.store.get_episode(episode_id)
        if record is None:  # pragma: no cover - the first node always saves the episode
            raise NotFound(f"episode {episode_id} was not recorded")
        if not result.get("__interrupt__") and record.status is EpisodeStatus.DONE:
            self.checkpointer.delete_thread(episode_id)
        if result.get("run_budget_exhausted"):
            # The episode is recorded (exit error); the run must stop, so the caller hears it.
            raise RunBudgetExhausted(record.exit_detail)
        return record

    def _resume(self, episode_id: str, answer: dict[str, Any]) -> EpisodeRecord:
        from langgraph.types import Command

        return self._invoke(episode_id, Command(resume=answer))

    def _stored(self, episode_id: str) -> EpisodeRecord:
        stored = self.world.store.get_episode(episode_id)
        if stored is None:
            raise NotFound(f"no episode {episode_id!r}")
        return stored

    def _stopped_at_execute(self, episode_id: str) -> bool:
        """True when the episode's thread is checkpointed just before ``execute``, not paused."""
        snapshot = self.graph.get_state(self._config(episode_id))
        return not snapshot.interrupts and tuple(snapshot.next) == ("execute",)

    def _paused(self, episode_id: str):  # -> tuple[EpisodeRecord, Proposal]
        stored = self._stored(episode_id)
        if stored.status is not EpisodeStatus.PENDING_REVIEW:
            return stored, stored.proposal
        if stored.proposal is None:  # pragma: no cover - a pause always stores its proposal
            raise NotPaused(f"episode {episode_id} has no proposal")
        snapshot = self.graph.get_state(self._config(episode_id))
        if not snapshot.interrupts:
            raise NotPaused(
                f"episode {episode_id} is pending review, but no paused graph was found "
                "in this checkpointer"
            )
        return stored, stored.proposal

    @staticmethod
    def _expired_answer(proposal: Any, book: Any) -> dict[str, Any]:
        best = book.best_ask(proposal.side)
        if best is not None and best.price > proposal.edge_bound_price:
            detail = f"the best ask {best.price} passed the edge bound {proposal.edge_bound_price}"
        else:
            detail = f"the proposal expired at {proposal.expires_at.isoformat()}"
        return {"kind": "expired", "detail": detail}


__all__ = ["EpisodeController", "NotPaused", "open_checkpointer", "state_client"]
