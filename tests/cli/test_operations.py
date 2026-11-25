"""``pmfa halt``, ``pmfa stage``, ``pmfa config``, ``pmfa report-status``, ``pmfa resolve``, ``pmfa state-server``."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
from typer.testing import CliRunner

from pmfa.cli.app import app
from pmfa.cli.state import config_toml
from pmfa.config import Config, load_config
from pmfa.records.base import RunMode
from pmfa.state.client import connect_stdio
from pmfa.state.service import OPERATIONS
from pmfa.state.store import SqliteStore
from tests.live.support import AS_OF

from .test_review import invoke, pause_one, stored

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- halt


def test_halt_sets_the_flag_cancels_and_resume_clears_it(live):
    halted = invoke("halt", "--reason", "going to lunch")
    assert halted.exit_code == 0, halted.output
    assert "halt flag set (shadow): going to lunch" in halted.output
    assert "cancelled 0 open order(s)" in halted.output
    assert "HALTED (shadow)" in invoke("halt", "--status").output
    assert "not halted" in invoke("halt", "--status", "--mode", "demo").output

    # A halted agent still runs its episode, and places nothing.
    live.last_look()
    result = invoke("shadow", "--once")
    assert "HALTED: going to lunch" in result.output and "halted" in result.output
    with stored(live) as store:
        assert store.list_orders() == []

    assert "halt cleared" in invoke("halt", "--resume").output
    assert "not halted (shadow)" in invoke("halt", "--status").output


# --------------------------------------------------------------------------- stage


def test_stage_status_prints_every_benchmark_with_its_numbers(live):
    pause_one(live)  # seeds the stage (approval) in the shadow database
    out = invoke("stage", "status").output
    assert out.startswith("stage: approval")
    for name in ("backtest -> shadow", "shadow -> approval", "approval -> autonomous"):
        assert name in out
    assert "approval -> autonomous: not met  <- gates the next stage" in out
    assert "min_trades: 100" in out and "min_markets: 200" in out


def test_stage_advance_refuses_an_unmet_benchmark_and_demote_records_why(live):
    pause_one(live)
    advance = runner.invoke(app, ["stage", "advance"])
    assert advance.exit_code == 1 and "not advanced" in advance.output
    demoted = invoke("stage", "demote", "--reason", "two loss stops")
    assert "demoted to shadow: demoted: two loss stops" in demoted.output
    with stored(live) as store:
        assert store.get_stage().stage.value == "shadow"


# --------------------------------------------------------------------------- config


def test_the_committed_pmfa_toml_is_exactly_the_built_in_defaults():
    path = ROOT / "pmfa.toml"
    assert load_config(path) == Config()
    assert path.read_text(encoding="utf-8") == config_toml(Config())


def test_config_show_prints_the_effective_configuration(live):
    result = invoke("config", "show")
    assert result.exit_code == 0
    assert result.output == config_toml(load_config(live.config_file))
    assert f'data_dir = "{live.data_dir}"' in result.output


def test_config_check_reports_each_problem(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text("[caps]\nmax_contracts_per_order = 10\n", encoding="utf-8")
    ok = invoke("config", "check", str(good))
    assert ok.exit_code == 0 and ok.output.strip().endswith(": ok")

    bad = tmp_path / "bad.toml"
    bad.write_text(
        "[caps]\nper_market_fraction = 0.5\nmax_contracts_per_order = 60\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["config", "check", str(bad)])
    assert result.exit_code == 1
    assert "2 problem(s)" in result.output
    assert "per_market_fraction cannot exceed caps.per_event_fraction" in result.output
    assert "order_group_contracts_limit" in result.output

    missing = runner.invoke(app, ["config", "check", str(tmp_path / "nope.toml")])
    assert missing.exit_code == 1 and "not found" in missing.output


# --------------------------------------------------------------------------- report and resolve


def test_report_status_covers_pnl_exits_fills_overrides_and_calibration(live):
    proposal_id = pause_one(live)
    invoke("review", "approve", proposal_id)
    out = invoke("report-status").output
    assert "pmfa status report (shadow)" in out
    assert "stage: approval" in out and "halt: not halted" in out
    assert "P&L (after fees" in out and "max drawdown" in out
    assert "exits: executed 1" in out
    assert "gate rejections (0)" in out
    assert "fill quality" in out and "filled" in out and "(100.0%)" in out
    assert "overrides: 0 of 1 review(s)" in out and "decisions: approve 1" in out
    assert "calibration by category" in out
    assert "episodes: 0" in invoke("report-status", "--mode", "demo").output


def test_report_status_shows_calibration_once_forecasts_resolve(live):
    from tests.episodes.support import calibrated_ledger

    pause_one(live)
    with stored(live) as store:
        entries = [e.model_copy(update={"mode": RunMode.SHADOW}) for e in calibrated_ledger(4)]
        store.add_ledger_entries(entries)
    out = invoke("report-status").output
    assert "fed_decision: 4 forecast(s), 4 market(s), 4 event(s); Brier" in out


def test_resolve_prints_what_the_resolver_reported(live, monkeypatch):
    from pmfa.live import loop as loop_module

    def resolver(world, *, now=None):  # type: ignore[no-untyped-def]
        assert now == AS_OF
        return ["KXFEDDECISION-26OCT-H0 settled yes"]

    monkeypatch.setattr(loop_module, "default_resolver", lambda: resolver)
    result = invoke("resolve")
    assert result.exit_code == 0 and "KXFEDDECISION-26OCT-H0 settled yes" in result.output
    monkeypatch.setattr(loop_module, "default_resolver", lambda: lambda world, now=None: [])
    assert "nothing resolved" in invoke("resolve").output


# --------------------------------------------------------------------------- state server


def test_the_state_server_lists_its_tools_over_stdio_and_can_search(tmp_path):
    data_dir = tmp_path / "data"
    config_file = tmp_path / "pmfa.toml"
    config_file.write_text(f'[paths]\ndata_dir = "{data_dir}"\n', encoding="utf-8")
    db = tmp_path / "state.sqlite"
    with SqliteStore(db) as store:
        store.set_halt("manual", datetime(2026, 11, 25, tzinfo=UTC))

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")]))
    env["PMFA_CONFIG"] = str(config_file)

    async def session():  # type: ignore[no-untyped-def]
        args = ["-m", "pmfa.cli.app", "state-server", "--db", str(db)]
        with anyio.fail_after(90):
            async with connect_stdio(sys.executable, args, env=env) as client:
                halt = await client.call_tool("get_halt", {})
                hits = await client.call_tool(
                    "search_events", {"query": "Fed decision", "as_of": "2026-11-25T00:00:00Z"}
                )
                return set(client.tools), halt, hits

    names, halt, hits = anyio.run(session)
    assert names == set(OPERATIONS)
    assert halt["halted"] is True and halt["reason"] == "manual"
    assert hits == []  # the knowledge port is attached (an empty index), not "unavailable"
