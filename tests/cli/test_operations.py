"""``pmfa halt``, ``pmfa stage``, ``pmfa config``, ``pmfa report-status``, ``pmfa resolve``, ``pmfa state-server``."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
from typer.testing import CliRunner

from pmfa.cli.app import app
from pmfa.cli.state import config_toml
from pmfa.config import Config, load_config
from pmfa.records.base import RunMode, Stage
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

    # A halted agent still runs its episode, and places nothing.
    live.last_look()
    result = invoke("shadow", "--once")
    assert "HALTED: going to lunch" in result.output and "halted" in result.output
    with stored(live) as store:
        assert store.list_orders() == []

    assert "halt cleared" in invoke("halt", "--resume").output
    assert "not halted (shadow)" in invoke("halt", "--status").output


def test_a_source_that_cannot_be_reached_is_one_line_not_a_traceback(live, monkeypatch):
    from pmfa.live import session as session_module
    from pmfa.world.evidence.http import FetchError

    url = "https://www.federalreserve.gov/json/ne-press.json"

    def unreachable(mode, config, *, review=False, models=None):  # type: ignore[no-untyped-def]
        raise FetchError(url, None, f"could not reach {url}: ConnectError")

    monkeypatch.setattr(session_module, "build_world", unreachable)
    result = runner.invoke(app, ["shadow", "--once"])
    assert result.exit_code == 1
    assert f"shadow mode stopped: could not reach {url}" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output


def halted_in(live, mode: RunMode) -> bool:  # type: ignore[no-untyped-def]
    with stored(live, mode) as store:
        return store.get_halt().halted


def test_halt_without_a_mode_stops_every_live_mode(live):
    halted = invoke("halt", "--reason", "stop everything")
    assert halted.exit_code == 0, halted.output
    assert "halt flag set (shadow): stop everything" in halted.output
    assert "halt flag set (demo): stop everything" in halted.output
    assert halted_in(live, RunMode.SHADOW) and halted_in(live, RunMode.DEMO)
    status = invoke("halt", "--status").output
    assert "HALTED (shadow)" in status and "HALTED (demo)" in status
    assert "HALTED (demo)" in invoke("halt", "--status", "--mode", "demo").output

    # --mode narrows: resuming demo leaves shadow halted.
    assert "halt cleared (demo)" in invoke("halt", "--resume", "--mode", "demo").output
    assert halted_in(live, RunMode.SHADOW) and not halted_in(live, RunMode.DEMO)
    assert "not halted (demo)" in invoke("halt", "--status").output
    assert "halt cleared (shadow)" in invoke("halt", "--resume").output
    assert not halted_in(live, RunMode.SHADOW)

    narrow = invoke("halt", "--mode", "shadow")
    assert "halt flag set (shadow)" in narrow.output and "(demo)" not in narrow.output
    assert halted_in(live, RunMode.SHADOW) and not halted_in(live, RunMode.DEMO)


def test_a_halt_sets_every_flag_before_any_exchange_and_survives_missing_demo_keys(
    live, monkeypatch
):
    from pmfa.errors import ConfigError
    from pmfa.live import session as session_module

    fake = session_module.build_world
    seen: list[tuple[RunMode, bool, bool]] = []

    def build(mode, config, *, review=False, models=None):  # type: ignore[no-untyped-def]
        mode = RunMode(mode)
        seen.append((mode, halted_in(live, RunMode.SHADOW), halted_in(live, RunMode.DEMO)))
        if mode is RunMode.DEMO:
            raise ConfigError("demo mode needs the demo API credentials in the environment")
        return fake(mode, config, review=review, models=models)

    monkeypatch.setattr(session_module, "build_world", build)
    result = runner.invoke(app, ["halt", "--reason", "no keys here"])
    assert result.exit_code == 0, result.output
    # Both flags were set before the first world (and so the first exchange client) was built.
    assert seen and all(shadow and demo for _, shadow, demo in seen)
    assert halted_in(live, RunMode.SHADOW) and halted_in(live, RunMode.DEMO)
    assert "demo: the flag is set; open orders were not cancelled" in result.output
    assert "demo API credentials" in result.output

    # Naming demo explicitly still reports the failed cancel as a failure.
    named = runner.invoke(app, ["halt", "--mode", "demo"])
    assert named.exit_code == 1 and "demo API credentials" in named.output


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


def tiny_backtest(root: Path) -> Path:
    """The evaluation tests' tiny scripted backtest (scoring pass only); returns its run directory."""
    from pmfa.evaluation.backtest import run_backtest
    from tests.evaluation.backtest.support import build_data, make_config, scripted

    config = make_config(root / "bt-data")
    build_data(config)
    run_dir = root / "runs" / "tiny"
    run_backtest(
        config, run_dir=run_dir, systems=["pipeline"], models=scripted(config), trade=False
    )
    return run_dir


def test_stage_reads_backtested_forecasts_from_a_backtest_run(live, tmp_path):
    run_dir = tiny_backtest(tmp_path)
    live.data_dir.mkdir(parents=True)
    with stored(live) as store:
        store.set_stage(Stage.BACKTEST, "fresh", AS_OF)
    alone = invoke("stage", "status").output
    assert "backtest -> shadow: not met  <- gates the next stage" in alone
    assert "  n_markets: 0" in alone

    out = invoke("stage", "status", "--from-run", str(run_dir)).output
    n = int(re.search(r"  n_markets: (\d+)", out).group(1))
    assert n > 0 and f"{n} backtested markets, needs 200" in out
    assert f"backtest forecasts from: {run_dir}" in out
    # The same run named twice is counted once.
    twice = invoke("stage", "status", "--from-run", str(run_dir), "--from-run", str(run_dir))
    assert f"  n_markets: {n}" in twice.output

    refused = runner.invoke(app, ["stage", "advance", "--from-run", str(run_dir)])
    assert refused.exit_code == 1 and f"{n} backtested markets, needs 200" in refused.output

    # With the market count lowered to what the run has, the run's positive skill advances it.
    live.config_file.write_text(
        live.config_file.read_text(encoding="utf-8") + f"[stages]\nbacktest_min_markets = {n}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["stage", "advance", "--from-run", str(run_dir)])
    assert result.exit_code == 0 and "advanced to shadow" in result.output, result.output
    with stored(live) as store:
        assert store.get_stage().stage is Stage.SHADOW


def test_stage_refuses_a_directory_that_is_not_a_backtest_run(live, tmp_path):
    result = runner.invoke(app, ["stage", "status", "--from-run", str(tmp_path / "nope")])
    assert result.exit_code == 1 and "scoring" in result.output


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
