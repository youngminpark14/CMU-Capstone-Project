"""The episode controller: run an episode for a trigger and return its record.

The same code runs in every mode; only the world behind the ports differs.
``run`` goes to an exit, or to the review pause (status ``PENDING_REVIEW``,
with the proposal and its card stored). ``resume`` applies a human review and
``expire`` expires the pending proposal; both continue the paused thread from
its checkpoint, which may have been written by another controller object (or
process) over the same store and checkpoint files.

An order whose outcome is unknown (sending raised an exchange error and the
order could not be found) does not end its episode: the episode and its
proposal are saved ``OUTCOME_UNKNOWN`` with no exit, and the thread waits in the
``hold`` node. ``reconcile`` asks again: it resumes the thread into the
``reconcile`` node, which looks the order up by its deterministic client order
id (the store, then ``Executor.find_order``) and never sends. Found, the order
is recorded and the episode ends by what the exchange did with it; the exchange
lists none, the proposal is cancelled and the episode ends ``error``, nothing
placed; the exchange cannot be asked, the episode stays ``OUTCOME_UNKNOWN`` and
waits again. The halt flag does not stop a lookup, since a lookup sends nothing.

An episode whose order may already be on the exchange is only ever reconciled,
never sent again, by whichever call reaches it (``run``, ``resume``, ``expire``,
``reconcile``): one saved ``OUTCOME_UNKNOWN``, and an unfinished one whose
thread stopped (the process died, or an exception escaped) at ``execute``,
``hold`` or ``reconcile``. A thread stopped at ``execute`` is continued there
marked ``continued``, so ``execute`` only looks the order up. The approval
behind a stopped thread is stale, so it is never acted on again. Nothing looks
for unfinished episodes on its own: a person names one to ``reconcile``.
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
from .pipeline import client_order_id, episode_id_for
from .trace import Tracer, dump

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.sqlite import SqliteSaver


class NotPaused(PmfaError):
    """The episode's graph is not waiting where the call needs it: not for review
    (``resume``, ``expire``), or, for an order whose outcome is unknown, no thread of it
    is in this checkpointer (``reconcile``)."""


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
        unless its order may already be on the exchange: then it is only reconciled
        (see the module docstring), or returned as stored when its thread is gone. Raises ``RunBudgetExhausted`` after recording the episode (exit
        ``error``) when the run's dollar cap was reached: the caller must stop the run.
        """
        episode_id = episode_id_for(self.world.mode, trigger)
        existing = self.world.store.get_episode(episode_id)
        if existing is not None:
            if self._in_doubt(existing):
                try:
                    return self._settle(existing)
                except NotPaused:
                    return existing
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
        no longer paused returns its record and does nothing. An episode whose order
        may already be on the exchange is only reconciled, whatever ``review`` says,
        and the review is not recorded (see the module docstring). Raises
        ``RunBudgetExhausted`` after recording, as ``run`` does.
        """
        from pmfa.execution.review import ReviewQueue

        stored = self._stored(episode_id)
        if self._in_doubt(stored):
            return self._settle(stored)
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

        An episode whose order may already be on the exchange is only reconciled (see
        the module docstring): an expiry cannot unsend an order.
        """
        from pmfa.execution.review import ReviewQueue

        stored = self._stored(episode_id)
        if self._in_doubt(stored):
            return self._settle(stored)
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

    def reconcile(self, episode_id: str) -> EpisodeRecord:
        """Ask the exchange again about an episode whose order may be on it. Sends nothing.

        Returns the episode as it then stands: ``DONE``, or still ``OUTCOME_UNKNOWN``
        when the exchange could not be asked. Any other episode is returned unchanged.
        Raises ``NotPaused`` when this checkpointer holds no thread for an
        ``OUTCOME_UNKNOWN`` episode, and ``NotFound`` for an unknown id.
        """
        stored = self._stored(episode_id)
        if not self._in_doubt(stored):
            return stored
        return self._settle(stored)

    def in_doubt(self, episode_id: str) -> bool:
        """True when the episode's order may already be on the exchange: it is then only reconciled."""
        return self._in_doubt(self._stored(episode_id))

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

    def _continue(self, episode_id: str) -> EpisodeRecord:
        """Continue a thread stopped at ``execute``, marked so that it only reconciles.

        The mark goes into the checkpoint, so a continuation that itself dies is
        continued the same way.
        """
        config = self._config(episode_id)
        self.graph.update_state(config, {"continued": True})
        if tuple(self.graph.get_state(config).next) != ("execute",):  # pragma: no cover
            raise NotPaused(f"episode {episode_id} no longer stops at execute")
        return self._invoke(episode_id, None)

    def _resume(self, episode_id: str, answer: dict[str, Any]) -> EpisodeRecord:
        from langgraph.types import Command

        return self._invoke(episode_id, Command(resume=answer))

    def _stored(self, episode_id: str) -> EpisodeRecord:
        stored = self.world.store.get_episode(episode_id)
        if stored is None:
            raise NotFound(f"no episode {episode_id!r}")
        return stored

    def _in_doubt(self, stored: EpisodeRecord) -> bool:
        """True when the episode's order may already be on the exchange, so it is only reconciled."""
        if stored.status is EpisodeStatus.OUTCOME_UNKNOWN:
            return True
        if stored.status is EpisodeStatus.DONE:
            return False
        nxt = tuple(self.graph.get_state(self._config(stored.episode_id)).next)
        return nxt in (("execute",), ("hold",), ("reconcile",))

    def _settle(self, stored: EpisodeRecord) -> EpisodeRecord:
        """Continue a thread whose order may be on the exchange, from wherever it stopped.

        Nothing here can send an order: ``execute`` is entered only marked
        ``continued`` (it then only looks the order up), ``hold`` only waits, and
        ``reconcile`` and ``record`` never send. A thread at ``hold`` is resumed into
        ``reconcile``.
        """
        episode_id = stored.episode_id
        nxt = tuple(self.graph.get_state(self._config(episode_id)).next)
        if nxt == ("execute",):
            return self._continue(episode_id)
        if nxt == ("hold",):
            return self._resume(episode_id, {"kind": "reconcile"})
        if nxt in (("reconcile",), ("record",)):
            return self._invoke(episode_id, None)
        # No second way to finish an episode without its thread: a person checks the exchange.
        coid = client_order_id(episode_id, stored.proposal) if stored.proposal else "unknown"
        raise NotPaused(
            f"episode {episode_id}'s order outcome is unknown and its checkpoint is missing, "
            f"so it cannot be reconciled here: look up client order id {coid} on the exchange"
        )

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
