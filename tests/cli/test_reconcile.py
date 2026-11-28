"""``pmfa reconcile`` and the unknown-outcome lines of ``pmfa shadow``, ``review`` and ``report-status``.

Each invocation builds a new world over the same files, as separate processes would;
the exchange (a paper account standing in for the exchange's own record) and the count
of orders sent to it outlive them, as a real exchange does.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from typer.testing import CliRunner

from pmfa.cli.app import app
from pmfa.live import session as session_module
from pmfa.records.base import RunMode
from pmfa.records.decision import ProposalStatus
from pmfa.records.episode import EpisodeStatus, Exit
from pmfa.state.store import SqliteStore
from pmfa.world.live import live_paths
from tests.episodes.support import doubtful, exchange_account

runner = CliRunner()


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args), catch_exceptions=False)


class Exchange:
    """What outlives one invocation: the exchange's record, what was sent to it, and how
    many lookups the next invocation's exchange fails."""

    def __init__(self) -> None:
        self.account: Any = None
        self.placed: list[Any] = []
        self.failures = 1
        self.crash = False


@pytest.fixture
def exchange(live, monkeypatch: pytest.MonkeyPatch) -> Exchange:  # type: ignore[no-untyped-def]
    state = Exchange()
    build = session_module.build_world

    def build_in_doubt(mode, config, *, review=False, models=None):  # type: ignore[no-untyped-def]
        world = build(mode, config, review=review, models=models)
        if state.account is None:
            state.account = exchange_account(world)
        world = doubtful(world, state.account, failures=state.failures, placed=state.placed)
        world.executor.crash = state.crash
        return world

    monkeypatch.setattr(session_module, "build_world", build_in_doubt)
    return state


def shadow_store() -> SqliteStore:
    from pmfa.config import load_config

    return SqliteStore(live_paths(load_config(), RunMode.SHADOW)[0])


def unknown_proposals(live) -> list[str]:  # type: ignore[no-untyped-def]
    with shadow_store() as store:
        return [p.proposal_id for p, _ in store.list_proposals(ProposalStatus.OUTCOME_UNKNOWN)]


def test_shadow_says_an_unknown_outcome_in_plain_words_and_reconcile_settles_it(live, exchange):
    live.last_look()
    shadow = invoke("shadow", "--once")
    assert shadow.exit_code == 0, shadow.output
    assert "outcome_unknown" in shadow.output
    assert "may stand on the exchange" in shadow.output
    assert "nothing more will be sent" in shadow.output
    assert "pmfa reconcile --mode shadow" in shadow.output
    [pid] = unknown_proposals(live)

    with shadow_store() as store:
        [episode] = store.list_episodes()
    traced = invoke("trace", episode.episode_id, "--db", str(live.data_dir / "state.sqlite"))
    assert traced.exit_code == 0, traced.output
    assert "none yet (outcome_unknown)" in traced.output
    assert "No order was sent" not in traced.output  # it may stand on the exchange
    assert "pmfa reconcile --mode shadow" in " ".join(traced.output.split())

    status = invoke("report-status")
    assert status.exit_code == 0, status.output
    assert "orders with an unknown outcome: 1" in status.output
    assert "pmfa reconcile --mode shadow" in status.output

    exchange.failures = 1  # the exchange still cannot answer
    waiting = invoke("reconcile")
    assert waiting.exit_code == 1, waiting.output
    assert re.search(rf"proposal {pid}: still unknown", waiting.output)
    assert unknown_proposals(live) == [pid]

    exchange.failures = 0
    settled = invoke("reconcile", "--mode", "shadow")
    assert settled.exit_code == 0, settled.output
    assert re.search(rf"proposal {pid}: executed", settled.output)
    assert unknown_proposals(live) == []
    assert len(exchange.placed) == 1

    with shadow_store() as store:
        [order] = store.list_orders()
        episode = store.get_episode(order.episode_id)
    assert episode.status is EpisodeStatus.DONE and episode.exit is Exit.EXECUTED

    nothing = invoke("reconcile")
    assert nothing.exit_code == 0 and "no order with an unknown outcome" in nothing.output
    assert "unknown outcome" not in invoke("report-status").output


def test_an_approval_whose_order_answer_is_lost_says_so_and_a_later_review_is_refused(
    live, exchange
):
    live.last_look()
    paused = invoke("shadow", "--once", "--review")
    assert paused.exit_code == 0, paused.output
    match = re.search(r"proposal (\S+): buy", paused.output)
    assert match, paused.output
    pid = match.group(1)

    approved = invoke("review", "approve", pid)
    assert approved.exit_code == 0, approved.output
    assert "order outcome unknown" in approved.output
    assert "pmfa reconcile --mode shadow" in approved.output
    assert unknown_proposals(live) == [pid]
    with shadow_store() as store:
        reviews = store.list_reviews()

    exchange.failures = 0
    for args in (("reject", pid, "--reason", "other"), ("approve", pid)):
        refused = invoke("review", *args)
        assert refused.exit_code == 1, refused.output
        assert "outcome is unknown, so a review cannot change it" in refused.output
        assert "pmfa reconcile --mode shadow" in refused.output
    with shadow_store() as store:
        assert store.list_reviews() == reviews  # no review was stored
    assert unknown_proposals(live) == [pid] and len(exchange.placed) == 1

    settled = invoke("reconcile")
    assert settled.exit_code == 0, settled.output
    assert re.search(rf"proposal {pid}: executed", settled.output)
    assert len(exchange.placed) == 1 and unknown_proposals(live) == []


def test_an_outcome_unknown_episode_whose_checkpoint_is_gone_names_its_client_order_id(
    live, exchange
):
    from pmfa.config import load_config

    live.last_look()
    assert invoke("shadow", "--once").exit_code == 0
    [pid] = unknown_proposals(live)
    live_paths(load_config(), RunMode.SHADOW)[1].unlink()  # the checkpoint file

    exchange.failures = 0
    result = invoke("reconcile")
    assert result.exit_code == 1, result.output
    assert "checkpoint is missing" in result.output and "client order id" in result.output
    assert "the next pass asks" not in result.output  # no pass can ask for this one
    assert unknown_proposals(live) == [pid] and len(exchange.placed) == 1

    passed = invoke("shadow", "--once")  # the live pass reports it as a note and goes on
    assert passed.exit_code == 0, passed.output
    assert "note:" in passed.output and "checkpoint is missing" in passed.output


def test_an_episode_that_died_while_sending_is_listed_and_settled_when_named(live, exchange):
    live.last_look()
    paused = invoke("shadow", "--once", "--review")
    pid = re.search(r"proposal (\S+): buy", paused.output).group(1)
    exchange.crash = True  # the order is sent, then the process dies during its lookup
    with pytest.raises(RuntimeError, match="died"):
        invoke("review", "approve", pid)
    exchange.crash, exchange.failures = False, 0
    with shadow_store() as store:
        [episode] = store.list_episodes()
    assert episode.status is not EpisodeStatus.DONE and len(exchange.placed) == 1

    with shadow_store() as store:
        proposal, reviews = store.get_proposal(pid), store.list_reviews()
    assert invoke("shadow", "--once", "--review").exit_code == 0  # a pass leaves it alone
    refused = invoke("review", "reject", pid, "--reason", "other")
    assert refused.exit_code == 1, refused.output
    assert f"--episode {episode.episode_id}" in refused.output
    with shadow_store() as store:
        assert store.get_episode(episode.episode_id) == episode
        assert store.get_proposal(pid) == proposal and store.list_reviews() == reviews
    assert len(exchange.placed) == 1
    traced = invoke("trace", episode.episode_id, "--db", str(live.data_dir / "state.sqlite"))
    assert "No order was sent" not in traced.output  # one was, and is not recorded yet

    status = invoke("report-status").output
    assert f"pmfa reconcile --mode shadow --episode {episode.episode_id}" in status
    assert "no order with an unknown outcome" in invoke("reconcile").output  # not swept

    settled = invoke("reconcile", "--episode", episode.episode_id)
    assert settled.exit_code == 0, settled.output
    assert re.search(rf"proposal {pid}: executed", settled.output)
    assert len(exchange.placed) == 1  # looked up, never sent again
    with shadow_store() as store:
        assert store.get_episode(episode.episode_id).exit is Exit.EXECUTED
    assert episode.episode_id not in invoke("report-status").output

    # A finished episode whose proposal was left approved (an error after the approval does
    # that) sent no order that could still stand: the refusal must not say one may have been.
    with shadow_store() as store:
        store.set_proposal_status(pid, ProposalStatus.APPROVED)
    late = invoke("review", "approve", pid)
    assert late.exit_code == 1, late.output
    assert "may have been sent" not in late.output and "not pending" in late.output


def test_reconcile_with_nothing_unknown_exits_zero(live):
    result = invoke("reconcile", "--mode", "demo")
    assert result.exit_code == 0, result.output
    assert "no order with an unknown outcome (demo)" in result.output


def test_a_pass_after_a_pass_that_died_while_sending_leaves_the_stopped_episode_alone(
    live, exchange
):
    live.last_look()
    exchange.crash = True  # the order is sent, then the process dies during its lookup
    with pytest.raises(RuntimeError, match="died"):
        invoke("shadow", "--once")
    exchange.crash, exchange.failures = False, 0
    with shadow_store() as store:
        [before] = store.list_episodes()
    assert before.status is EpisodeStatus.RUNNING and len(exchange.placed) == 1

    again = invoke("shadow", "--once")  # finds the same trigger: another process may own it
    assert again.exit_code == 0, again.output
    with shadow_store() as store:
        assert store.get_episode(before.episode_id) == before
    assert len(exchange.placed) == 1

    settled = invoke("reconcile", "--episode", before.episode_id)
    assert settled.exit_code == 0, settled.output
    with shadow_store() as store:
        assert store.get_episode(before.episode_id).exit is Exit.EXECUTED
    assert len(exchange.placed) == 1


@pytest.mark.parametrize("later", [("reject", "--reason", "other"), ("resize", "--contracts", "1")])
def test_a_different_review_after_an_approval_that_never_resumed_sends_nothing(
    live, exchange, monkeypatch: pytest.MonkeyPatch, later: tuple[str, ...]
):
    from pmfa.episode.controller import EpisodeController

    live.last_look()
    exchange.failures = 0
    paused = invoke("shadow", "--once", "--review")
    pid = re.search(r"proposal (\S+): buy", paused.output).group(1)

    real = EpisodeController._resume
    calls = {"n": 0}

    def dies_once(self, episode_id, answer):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:  # the approval is stored, then the process dies
            raise RuntimeError("died before the graph resumed")
        return real(self, episode_id, answer)

    monkeypatch.setattr(EpisodeController, "_resume", dies_once)
    with pytest.raises(RuntimeError, match="died"):
        invoke("review", "approve", pid)
    with shadow_store() as store:
        assert store.get_proposal(pid)[1] is ProposalStatus.APPROVED

    result = invoke("review", later[0], pid, *later[1:])
    assert result.exit_code == 0, result.output
    assert "expired" in result.output
    assert exchange.placed == []
    with shadow_store() as store:
        [episode] = store.list_episodes()
    assert episode.status is EpisodeStatus.DONE and episode.exit is Exit.EXPIRED


def ledger_dies_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next ``record`` dies after ``execute`` stored the order: no ledger entry is written."""
    real = SqliteStore.add_ledger_entries
    calls = {"n": 0}

    def dies_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("died in record")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SqliteStore, "add_ledger_entries", dies_once)


def test_an_episode_that_died_in_record_after_its_order_is_settled_when_named(
    live, exchange, monkeypatch: pytest.MonkeyPatch
):
    live.last_look()
    exchange.failures = 0
    ledger_dies_once(monkeypatch)
    with pytest.raises(RuntimeError, match="died in record"):
        invoke("shadow", "--once")
    with shadow_store() as store:
        [episode] = store.list_episodes()
    assert episode.status is EpisodeStatus.RUNNING and len(exchange.placed) == 1

    settled = invoke("reconcile", "--episode", episode.episode_id)
    assert settled.exit_code == 0, settled.output
    with shadow_store() as store:
        assert store.get_episode(episode.episode_id).exit is Exit.EXECUTED
    assert len(exchange.placed) == 1


def test_a_reconcile_that_died_in_record_is_finished_by_the_next_one(
    live, exchange, monkeypatch: pytest.MonkeyPatch
):
    live.last_look()
    assert invoke("shadow", "--once").exit_code == 0  # the outcome is unknown
    assert len(unknown_proposals(live)) == 1

    exchange.failures = 0
    ledger_dies_once(monkeypatch)
    with pytest.raises(RuntimeError, match="died in record"):
        invoke("reconcile")

    with shadow_store() as store:
        [episode] = store.list_episodes()
    assert episode.status is EpisodeStatus.OUTCOME_UNKNOWN  # record saved nothing new
    settled = invoke("reconcile", "--episode", episode.episode_id)
    assert settled.exit_code == 0, settled.output
    with shadow_store() as store:
        assert store.get_episode(episode.episode_id).exit is Exit.EXECUTED
    assert len(exchange.placed) == 1


def test_an_approval_whose_process_died_in_the_gate_is_ended_by_reconcile(
    live, exchange, monkeypatch: pytest.MonkeyPatch
):
    from pmfa.execution import halt as halt_module

    live.last_look()
    exchange.failures = 0
    paused = invoke("shadow", "--once", "--review")
    pid = re.search(r"proposal (\S+): buy", paused.output).group(1)

    real = halt_module.enforce
    calls = {"n": 0}

    def dies_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:  # the process dies in the gate after the approval
            raise RuntimeError("died in the gate")
        return real(*args, **kwargs)

    monkeypatch.setattr(halt_module, "enforce", dies_once)
    with pytest.raises(RuntimeError, match="died in the gate"):
        invoke("review", "approve", pid)
    with shadow_store() as store:
        [episode] = store.list_episodes()
    assert episode.status is EpisodeStatus.PENDING_REVIEW

    settled = invoke("reconcile", "--episode", episode.episode_id)
    assert settled.exit_code == 0, settled.output
    assert "expired" in settled.output
    with shadow_store() as store:
        ended = store.get_episode(episode.episode_id)
    assert ended.status is EpisodeStatus.DONE and ended.exit is Exit.EXPIRED
    assert exchange.placed == []
