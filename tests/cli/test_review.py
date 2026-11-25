"""``pmfa shadow`` / ``pmfa demo`` then ``pmfa review``: pause in one invocation, decide in the next."""

from __future__ import annotations

import re
from datetime import timedelta

from typer.testing import CliRunner

from pmfa.cli.app import app
from pmfa.records.base import RunMode
from pmfa.records.decision import ProposalStatus
from pmfa.records.episode import Exit
from pmfa.state.store import SqliteStore
from pmfa.world.live import live_paths
from tests.live.support import AS_OF

runner = CliRunner()


def invoke(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args), catch_exceptions=False)


def pause_one(live, mode: RunMode = RunMode.SHADOW) -> str:  # type: ignore[no-untyped-def]
    """Run one pass that pauses the minutes' proposal; return its id."""
    live.last_look(mode)
    args = ["shadow", "--once", "--review"] if mode is RunMode.SHADOW else ["demo", "--once"]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    match = re.search(r"proposal (prop\S*|\S+): buy", result.output)
    assert match, result.output
    return match.group(1)


def stored(live, mode: RunMode = RunMode.SHADOW) -> SqliteStore:  # type: ignore[no-untyped-def]
    from pmfa.config import load_config

    return SqliteStore(live_paths(load_config(), mode)[0])


def test_shadow_says_what_it_will_not_do_before_anything_and_where_proposals_wait(live):
    live.last_look()
    result = invoke("shadow", "--once", "--review")
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("No order is ever sent to any exchange") < out.index("new_document")
    assert "simulated against the live order book" in out
    assert "pending_review" in out and "buy" in out
    assert "pmfa review list --mode shadow" in out
    assert str(live.data_dir / "state.sqlite") in out


def test_review_list_show_and_approve_across_two_invocations(live):
    proposal_id = pause_one(live)

    listed = invoke("review", "list")
    assert proposal_id in listed.output and "[pending]" in listed.output

    shown = invoke("review", "show", proposal_id)
    assert shown.exit_code == 0
    card = shown.output
    assert "Fed decision in Dec 2026?" in card  # the event
    assert "KXFEDDECISION-26DEC-C25" in card  # the market
    assert "against a price of" in card and "edge after fees" in card
    assert "cost up to $" in card
    assert "routed to review because" in card and "cost_under_risk_unit" in card
    assert re.search(r"1\. \[2026-11-\d\d \d\d:\d\d UTC\] federalreserve\.gov", card)

    live.start = AS_OF + timedelta(minutes=2)  # a later process, still before expiry
    approved = invoke("review", "approve", proposal_id)
    assert approved.exit_code == 0, approved.output
    assert "executed" in approved.output and "fill" in approved.output
    with stored(live) as store:
        assert store.get_proposal(proposal_id)[1] is ProposalStatus.EXECUTED
        [order] = store.list_orders()
        assert order.filled_contracts > 0
    assert invoke("review", "list").output.startswith("no pending proposals")


def test_resize_places_the_smaller_order(live):
    proposal_id = pause_one(live)
    result = invoke(
        "review", "resize", proposal_id, "--contracts", "1", "--reason", "size_too_large"
    )
    assert result.exit_code == 0, result.output
    assert "executed" in result.output
    with stored(live) as store:
        [order] = store.list_orders()
        assert order.requested_contracts == 1
        [review] = store.list_reviews()
        assert review.reason_code.value == "size_too_large"


def test_reject_needs_a_known_reason_code_and_places_nothing(live):
    proposal_id = pause_one(live)
    bad = runner.invoke(app, ["review", "reject", proposal_id, "--reason", "bored"])
    assert bad.exit_code != 0 and "not a reason code" in bad.output
    result = invoke(
        "review",
        "reject",
        proposal_id,
        "--reason",
        "disagree_with_forecast",
        "--note",
        "too hawkish",
    )
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output
    with stored(live) as store:
        assert store.list_orders() == []
        [review] = store.list_reviews()
        assert review.note == "too hawkish"
        episode = store.get_episode(store.get_proposal(proposal_id)[0].episode_id)
        assert episode.exit is Exit.REJECTED_BY_HUMAN


def test_a_decision_after_expiry_reports_expired_and_places_nothing(live):
    proposal_id = pause_one(live)
    live.start = AS_OF + timedelta(minutes=30)
    result = invoke("review", "approve", proposal_id)
    assert result.exit_code == 0, result.output
    assert "expired" in result.output
    with stored(live) as store:
        assert store.list_orders() == []


def test_a_resize_that_grows_the_trade_is_refused_and_the_proposal_still_waits(live):
    proposal_id = pause_one(live)
    result = runner.invoke(app, ["review", "resize", proposal_id, "--contracts", "999"])
    assert result.exit_code == 1 and "not decided" in result.output
    assert proposal_id in invoke("review", "list").output


def test_demo_says_every_order_needs_approval_and_pauses_for_it(live):
    proposal_id = pause_one(live, RunMode.DEMO)
    assert live.built == [RunMode.DEMO]
    listed = invoke("review", "list", "--mode", "demo")
    assert proposal_id in listed.output
    assert "no pending proposals" in invoke("review", "list").output  # shadow's queue is apart
    approved = invoke("review", "approve", proposal_id, "--mode", "demo")
    assert "executed" in approved.output, approved.output


def test_demo_prints_its_promise_then_refuses_without_credentials(tmp_path, monkeypatch):
    toml = tmp_path / "pmfa.toml"
    toml.write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    monkeypatch.setenv("PMFA_CONFIG", str(toml))
    for name in ("KALSHI_DEMO_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY", "KALSHI_DEMO_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)
    result = runner.invoke(app, ["demo", "--once"])
    assert result.exit_code == 2
    assert "Every order needs your approval" in result.output
    assert "KALSHI_DEMO_KEY_ID" in result.output
    assert result.output.index("mock funds") < result.output.index("cannot start demo mode")
