"""``pmfa ask``: the command line around one asked run, on the constructed Bitcoin world.

The world and the Coinbase answers are the constructed ones of ``tests.live.ask_support``,
put behind ``build_world`` and the snapshot's fetcher; nothing touches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pmfa.cli import ask as ask_command
from pmfa.cli.app import app
from pmfa.live import ask as ask_module
from pmfa.live import session as session_module
from pmfa.records.decision import ProposalStatus
from pmfa.records.episode import Exit
from pmfa.state.store import SqliteStore
from tests.live.ask_support import ATM, Built, FakeFetcher, ask_world, coinbase_pages, config_toml

runner = CliRunner()


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Built:
    config_file = tmp_path / "pmfa.toml"
    config_file.write_text(config_toml(tmp_path / "data"), encoding="utf-8")
    monkeypatch.setenv("PMFA_CONFIG", str(config_file))
    state = Built()

    def fake_build_world(mode, config, *, review=False, models=None):  # type: ignore[no-untyped-def]
        return ask_world(config, review=review, built=state, at=state.at)

    def fake_fetcher(config):  # type: ignore[no-untyped-def]
        state.snapshots.append(FakeFetcher(coinbase_pages()))
        return state.snapshots[-1]

    monkeypatch.setattr(session_module, "build_world", fake_build_world)
    monkeypatch.setattr(ask_module, "snapshot_fetcher", fake_fetcher)
    return state


def test_with_no_terminal_and_no_decide_it_exits_2_before_any_fetch_or_model_call(built):
    result = runner.invoke(app, ["ask", "KXBTCD"])
    assert result.exit_code == 2
    assert "--decide" in result.output
    assert built.worlds == [] and built.models == [] and built.snapshots == []


def test_an_unknown_series_exits_2_naming_the_configured_ones(built):
    result = runner.invoke(app, ["ask", "KXNOPE", "--decide", "reject"])
    assert result.exit_code == 2
    assert "KXNOPE" in result.output and "KXBTCD" in result.output
    assert "KXFEDDECISION" in result.output and built.worlds == []


def test_decide_approve_prints_each_step_and_ends_executed(built, tmp_path: Path):
    result = runner.invoke(app, ["ask", "KXBTCD", "--decide", "approve"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("nothing is ever sent to any exchange") < out.index("Event:")
    for line in ("Market: $59,800 or above", "Rules: If the simple average", "Sources kept: 2"):
        assert line in out
    assert "The agent's answer: YES" in out
    assert "Prices on Kalshi now, read after the episode: YES $0.49" in out
    assert "Proposal: buy" in out and "routed to review because" in out
    assert "executed" in out and "(simulated)" in out
    assert "pmfa resolve" in out and "pmfa report-status" in out
    folders = list((tmp_path / "runs" / "ask").iterdir())
    assert [f.name for f in folders] == [f"20261128T052200Z-{ATM}"]
    assert str(folders[0]) in out


def test_on_a_terminal_the_person_is_asked_and_a_resize_is_carried_out(
    built, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(ask_command, "interactive", lambda: True)
    result = runner.invoke(app, ["ask", "KXBTCD"], input="maybe\nresize 3\n")
    assert result.exit_code == 0, result.output
    assert "Type approve, reject, or resize" in result.output
    with SqliteStore(tmp_path / "data" / "state.sqlite") as store:
        [episode] = store.list_episodes()
        [order] = store.list_orders()
    assert episode.exit is Exit.EXECUTED and order.requested_contracts == 3


def test_decide_reject_ends_rejected_by_human_with_no_order(built, tmp_path: Path):
    result = runner.invoke(app, ["ask", "kxbtcd", "--decide", "reject"])  # any letter case
    assert result.exit_code == 0, result.output
    assert "--decide reject: rejecting." in result.output
    with SqliteStore(tmp_path / "data" / "state.sqlite") as store:
        [episode] = store.list_episodes()
        assert episode.exit is Exit.REJECTED_BY_HUMAN and store.list_orders() == []


def test_a_resize_out_of_range_is_asked_again(
    built, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(ask_command, "interactive", lambda: True)
    result = runner.invoke(app, ["ask", "KXBTCD"], input="resize 999\nresize 0\nresize 2\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("A resize takes 1 to ") == 2
    with SqliteStore(tmp_path / "data" / "state.sqlite") as store:
        [episode] = store.list_episodes()
        [order] = store.list_orders()
    assert episode.exit is Exit.EXECUTED and order.requested_contracts == 2


def test_no_answer_at_the_prompt_rejects_and_points_at_no_later_review(
    built, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(ask_command, "interactive", lambda: True)
    result = runner.invoke(app, ["ask", "KXBTCD"], input="")  # Ctrl-D: end of input
    assert result.exit_code == 0, result.output
    assert "No answer: rejecting." in result.output
    assert "pmfa review approve" not in result.output
    with SqliteStore(tmp_path / "data" / "state.sqlite") as store:
        [episode] = store.list_episodes()
        assert episode.exit is Exit.REJECTED_BY_HUMAN and store.list_orders() == []
        assert store.list_proposals(ProposalStatus.PENDING) == []


def test_no_event_in_the_window_exits_1_and_says_why(built):
    from datetime import UTC, datetime

    built.at = datetime(2026, 11, 28, 5, 55, tzinfo=UTC)
    result = runner.invoke(app, ["ask", "KXBTCD", "--decide", "approve"])
    assert result.exit_code == 1
    assert "closes at 06:00 UTC" in result.output and "--market" in result.output
    assert built.models[0].calls == []
