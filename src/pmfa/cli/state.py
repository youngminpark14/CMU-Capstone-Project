"""``pmfa state-server``, ``pmfa resolve``, ``pmfa config show|check`` and ``pmfa report-status``.

- ``state-server [--db PATH]`` serves the state database over MCP on stdio,
  with the knowledge index and the read-only production exchange when they can
  be built without credentials, so a reviewer's MCP client can search and read
  rules as well as browse episodes. stdout carries protocol messages only.
- ``resolve [--mode]`` runs the outcome resolver once and prints what resolved.
- ``config show`` prints the effective configuration as TOML (``pmfa.toml`` at
  the repository root is this output for the built-in defaults, and a test keeps
  the two equal); ``config check [PATH]`` validates a file and prints the problems.
- ``report-status [--mode]`` prints the monitoring report the spec asks for in
  place of a dashboard: P&L, drawdown, exits and gate rejections by reason, fill
  quality, overrides, calibration by category, the stage and the halt state.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from pmfa.live.session import LiveMode

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.ports import StateStore
    from pmfa.records.base import RunMode

config_app = typer.Typer(
    name="config", help="Show the effective configuration or check a file.", no_args_is_help=True
)

MODE_OPTION = typer.Option(LiveMode.SHADOW, "--mode", help="Which live mode's world.")
CHECK_PATH = typer.Argument(
    None, help="The file to check. Default: $PMFA_CONFIG, else ./pmfa.toml, else the defaults."
)
DB_OPTION = typer.Option(
    None, "--db", help="The state database. Default: the shadow mode's database."
)

# --------------------------------------------------------------------------- config as TOML

HEADER = (
    "# pmfa configuration: every limit, threshold, source, model and followed series.\n"
    "# The built-in defaults as `pmfa config show` writes them; a test keeps this file equal to them.\n"
    "# Change a default in src/pmfa/config.py and regenerate; local overrides go in $PMFA_CONFIG.\n"
)
SECTION_NOTES = {
    "follow": "Followed series, how each becomes an outcome space, and the blocklist.",
    "sources": "The official evidence sources and how politely they are fetched.",
    "triggers": "What starts an episode: price moves, schedule ticks, scoring horizons.",
    "retrieval": "The events, evidence and ledger indexes: passage size, top-k, ranking.",
    "models": "Model and effort per role slot, prices and cutoffs, the API key variable.",
    "budget": "Per-episode and per-run caps on model calls, seconds and dollars.",
    "forecast": "The scenario tree: depth, beam, coverage floor, quantiles.",
    "sizing": "Fractional Kelly and the minimum edge after fees.",
    "caps": "Hard limits the gate enforces: per order, market, event, day, drawdown, liquidity.",
    "routing": "When a proposal may go auto instead of to human review.",
    "oversight": "Proposal expiry, fill-failure halt, calibration halt, the trading day.",
    "stages": "The autonomy ladder's benchmarks.",
    "execution": "The notional bankroll and the backtest fill assumptions.",
    "kalshi": "Exchange hosts, demo credential variable names (never values), request rate.",
    "state": "How the controller reaches the roles' shared state.",
    "evaluation": "The backtest window, systems compared, bootstrap and the canary.",
    "paths": "Where data, the state database, indexes and runs live.",
}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return json.dumps(value.value)
    if isinstance(value, Decimal):
        # Written without trailing zeros, so a file read back and written again is unchanged.
        return format(value.normalize(), "f")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def _key(key: Any) -> str:
    text = key.value if isinstance(key, Enum) else str(key)
    if text and all(c.isalnum() or c in "-_" for c in text):
        return text
    return json.dumps(text)


def _is_table(value: Any) -> bool:
    return isinstance(value, dict)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _table(name: str, data: dict[Any, Any], out: list[str], *, array: bool = False) -> None:
    out.append(f"[[{name}]]" if array else f"[{name}]")
    nested = []
    for key, value in data.items():
        if value is None:
            continue
        if _is_table(value) or _is_table_array(value):
            nested.append((key, value))
        else:
            out.append(f"{_key(key)} = {_toml_value(value)}")
    for key, value in nested:
        child = f"{name}.{_key(key)}"
        if _is_table_array(value):
            for item in value:
                out.append("")
                _table(child, item, out, array=True)
        elif all(_is_table(v) for v in value.values()) and value:
            for sub, item in value.items():
                out.append("")
                _table(f"{child}.{_key(sub)}", item, out)
        else:
            out.append("")
            _table(child, value, out)


def config_toml(config: Config) -> str:
    """The configuration written out as TOML, one commented table per section."""
    data = config.model_dump(mode="python")
    out: list[str] = [HEADER.rstrip("\n")]
    for section, value in data.items():
        out.append("")
        out.append(f"# {SECTION_NOTES.get(section, section)}")
        _table(section, value, out)
    return "\n".join(out) + "\n"


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration (defaults, then pmfa.toml or $PMFA_CONFIG) as TOML."""
    from pmfa.config import load_config
    from pmfa.errors import ConfigError

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(config_toml(config), nl=False)


def config_problems(exc: Exception) -> list[str]:
    """One line per problem behind a ``ConfigError`` (field errors and failed consistency checks)."""
    from pydantic import ValidationError

    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return [str(exc)]
    lines = []
    for error in cause.errors():
        message = str(error["msg"]).removeprefix("Value error, ")
        where = ".".join(str(part) for part in error["loc"])
        if error["type"] == "value_error" and not where:
            lines.extend(part.strip() for part in message.split("; ") if part.strip())
        else:
            lines.append(f"{where}: {message}" if where else message)
    return lines


@config_app.command("check")
def config_check(
    path: Path | None = CHECK_PATH,
) -> None:
    """Validate a configuration file and print its problems (exit 1 when there are any)."""
    import os

    from pmfa.config import CONFIG_ENV, DEFAULT_CONFIG_FILE, load_config
    from pmfa.errors import ConfigError

    shown = path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_FILE
    try:
        load_config(path)
    except ConfigError as exc:
        problems = config_problems(exc)
        typer.echo(f"{shown}: {len(problems)} problem(s)")
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(1) from None
    typer.echo(f"{shown}: ok")


# --------------------------------------------------------------------------- state server


def state_server(
    db: Path | None = DB_OPTION,
) -> None:
    """Serve the state database over MCP on stdio (protocol messages only on stdout)."""
    import contextlib
    import logging
    import sys

    from pmfa.config import load_config
    from pmfa.records.base import RunMode
    from pmfa.state import mcp_compat
    from pmfa.state.server import build_server
    from pmfa.state.service import StateService
    from pmfa.state.store import SqliteStore
    from pmfa.world.live import live_paths

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    log = logging.getLogger("pmfa.state-server")
    closers = []
    # Anything a library prints while the ports are built goes to stderr, not the protocol stream.
    with contextlib.redirect_stdout(sys.stderr):
        config = load_config()
        target = db or live_paths(config, RunMode.SHADOW)[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(target)
        closers.append(store.close)
        knowledge = exchange = None
        try:
            from pmfa.knowledge.retriever import ChromaKnowledge

            knowledge = ChromaKnowledge(config, path=config.paths.index_dir())
            closers.append(knowledge.close)
        except Exception as exc:  # the server still serves stored state without it
            log.warning("knowledge index not available: %s", exc)
        try:
            from pmfa.world.clocks import SystemClock
            from pmfa.world.kalshi.production import production_exchange

            exchange = production_exchange(config, SystemClock())
            closers.append(exchange.client.close)
        except Exception as exc:
            log.warning("exchange not available: %s", exc)
        server = build_server(StateService(store, knowledge, exchange))
    try:
        mcp_compat.run_stdio(server)
    finally:
        for close in reversed(closers):
            with contextlib.suppress(Exception):
                close()


# --------------------------------------------------------------------------- resolve


def resolve(mode: LiveMode = MODE_OPTION) -> None:
    """Run the outcome resolver once and print what resolved."""
    from pmfa.config import load_config
    from pmfa.errors import ConfigError
    from pmfa.live import session as live_session
    from pmfa.live.loop import default_resolver, describe_resolution

    try:
        resolver = default_resolver()
    except ModuleNotFoundError as exc:
        typer.echo(f"the outcome resolver is not available: {exc}", err=True)
        raise typer.Exit(1) from None
    try:
        with live_session.open_session(mode.run_mode, load_config()) as s:
            lines = describe_resolution(resolver(s.world, now=s.world.clock.now()))
    except ConfigError as exc:
        typer.echo(f"cannot open {mode.value} mode: {exc}", err=True)
        raise typer.Exit(2) from None
    if not lines:
        typer.echo("nothing resolved")
    for line in lines:
        typer.echo(line)


# --------------------------------------------------------------------------- report


def _trade_rows(store: StateStore, mode: RunMode) -> list[Any]:
    from pmfa.evaluation.rows import TradeRow
    from pmfa.records.decision import RoutingTier

    settlements = {t: (side, at) for t, side, at in store.list_settlements()}
    rows = []
    episodes: dict[str, Any] = {}
    for order in store.list_orders():
        if order.episode_id not in episodes:
            episodes[order.episode_id] = store.get_episode(order.episode_id)
        episode = episodes[order.episode_id]
        if episode is None or episode.mode is not mode:
            continue
        routing = episode.routing
        for f in order.fills:
            if f.contracts <= 0:
                continue
            result, settled_at = settlements.get(f.market_ticker, (None, None))
            if settled_at is not None and settled_at < f.filled_at:
                result, settled_at = None, None
            rows.append(
                TradeRow(
                    episode_id=order.episode_id,
                    event_ticker=episode.event_ticker or "",
                    market_ticker=f.market_ticker,
                    side=f.side,
                    contracts=f.contracts,
                    price=f.price,
                    fee=f.fee,
                    tier=routing.tier if routing is not None else RoutingTier.APPROVE,
                    would_be_auto=routing.would_be_auto if routing is not None else False,
                    filled_at=f.filled_at,
                    result=result,
                    settled_at=settled_at,
                )
            )
    return rows


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "none"


def status_report(store: StateStore, config: Config, mode: RunMode) -> list[str]:
    """The plain-text monitoring report for one mode's store."""
    from pmfa.evaluation.metrics import (
        brier_score,
        brier_skill,
        expected_calibration_error,
        market_brier_score,
        sample_size,
    )
    from pmfa.evaluation.pnl import equity_curve, max_drawdown, pnl_summary
    from pmfa.evaluation.rows import ForecastRow
    from pmfa.evaluation.summary import episode_tallies
    from pmfa.records.decision import ReviewDecision

    bankroll = config.execution.notional_bankroll
    lines = [f"pmfa status report ({mode.value})"]
    stage, halt = store.get_stage(), store.get_halt()
    lines.append(f"stage: {stage.stage.value}" + (f" ({stage.reason})" if stage.reason else ""))
    lines.append(f"halt: HALTED - {halt.reason}" if halt.halted else "halt: not halted")

    trades = _trade_rows(store, mode)
    pnl = pnl_summary(trades)
    drawdown = max_drawdown(equity_curve(trades, bankroll), bankroll)
    marks = store.equity_series()
    lines.append("")
    lines.append("P&L (after fees, settled trades only)")
    lines.append(
        f"  total ${pnl.total} over {pnl.n_settled} settled of {pnl.n_trades} trade(s); "
        f"fees ${pnl.fees}; still open: ${pnl.open_cost} at cost"
    )
    for event, value in sorted(pnl.by_event.items()):
        lines.append(f"  {event}: ${value}")
    lines.append(
        f"  max drawdown {drawdown.fraction:.4f} (${drawdown.dollars}) from a ${bankroll} bankroll"
    )
    if marks:
        lines.append(f"  last equity mark ${marks[-1][1]} at {marks[-1][0]:%Y-%m-%d %H:%M} UTC")

    episodes = [e for e in store.list_episodes(limit=1_000_000) if e.mode is mode]
    tallies = episode_tallies(episodes)
    lines.append("")
    lines.append(f"episodes: {tallies.n_episodes}")
    lines.append(f"  exits: {_counts_line(tallies.exits)}")
    lines.append(
        f"  gate rejections ({tallies.n_gate_rejected}): {_counts_line(tallies.gate_rejections)}"
    )
    lines.append(
        f"  routing: {_counts_line(tallies.routing_tiers)}; would-be auto {tallies.n_would_be_auto}"
    )
    if tallies.cost_usd.mean is not None:
        lines.append(
            f"  cost per episode: mean ${tallies.cost_usd.mean:.4f}, max ${tallies.cost_usd.max}"
        )

    ids = {e.episode_id for e in episodes}
    orders = [o for o in store.list_orders() if o.episode_id in ids]
    requested = sum(o.requested_contracts for o in orders)
    filled = sum((o.filled_contracts for o in orders), Decimal(0))
    fills = [f for o in orders for f in o.fills]
    improvement = sum(
        ((o.limit_price - f.price) * f.contracts for o in orders for f in o.fills),
        Decimal(0),
    )
    lines.append("")
    lines.append("fill quality")
    lines.append(
        f"  orders: {len(orders)} ({_counts_line(Counter(o.status.value for o in orders))})"
    )
    if requested:
        lines.append(
            f"  filled {filled} of {requested} contracts ({float(filled / requested):.1%})"
        )
    if filled:
        lines.append(
            f"  mean price below the limit {improvement / filled:.4f} per contract; "
            f"fees ${sum((f.fee for f in fills), Decimal(0))}"
        )

    proposals = {p.proposal_id for p, _ in store.list_proposals() if p.episode_id in ids}
    reviews = [r for r in store.list_reviews() if r.proposal_id in proposals]
    overridden = [r for r in reviews if r.decision is not ReviewDecision.APPROVE]
    lines.append("")
    lines.append(f"overrides: {len(overridden)} of {len(reviews)} review(s)")
    lines.append(f"  decisions: {_counts_line(Counter(r.decision.value for r in reviews))}")
    codes = Counter(r.reason_code.value for r in reviews if r.reason_code is not None)
    lines.append(f"  reason codes: {_counts_line(dict(codes))}")

    lines.append("")
    lines.append("calibration by category (resolved pipeline forecasts)")
    by_category: dict[str, list[ForecastRow]] = {}
    for e in store.ledger(system="pipeline"):
        if e.mode is not mode or e.outcome is None or e.market_price is None:
            continue
        by_category.setdefault(e.category, []).append(
            ForecastRow(
                run_id="ledger",
                system=e.system,
                event_ticker=e.event_ticker,
                market_ticker=e.market_ticker,
                category=e.category,
                trigger_id=e.episode_id,
                as_of=e.as_of,
                probability=e.probability,
                market_price=e.market_price,
                outcome=e.outcome,
            )
        )
    if not by_category:
        lines.append("  none resolved yet")
    for category, rows in sorted(by_category.items()):
        size = sample_size(rows)
        lines.append(
            f"  {category}: {size.n_rows} forecast(s), {size.n_markets} market(s), "
            f"{size.n_events} event(s); Brier {_num(brier_score(rows))}, market "
            f"{_num(market_brier_score(rows))}, skill {_num(brier_skill(rows))}, "
            f"ECE {_num(expected_calibration_error(rows, bins=config.evaluation.reliability_bins))}"
        )
    return lines


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def report_status(mode: LiveMode = MODE_OPTION) -> None:
    """Print the monitoring report: P&L, drawdown, exits, rejections, fills, overrides, calibration."""
    from pmfa.config import load_config
    from pmfa.live.session import open_store

    config = load_config()
    with open_store(mode.run_mode, config) as store:
        for line in status_report(store, config, mode.run_mode):
            typer.echo(line)


def register(app: typer.Typer) -> None:
    app.command("state-server")(state_server)
    app.command("resolve")(resolve)
    app.add_typer(config_app, name="config")
    app.command("report-status")(report_status)


__all__ = ["config_app", "config_toml", "register", "status_report"]
