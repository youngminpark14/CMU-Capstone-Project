"""``pmfa shadow`` and ``pmfa demo``: run the live modes in the foreground, once or in a loop.

Each command first says what it will and will not do, then builds the world,
runs one pass (``--once``, the default) or passes ``--interval`` seconds apart
(``--loop``, until Ctrl-C), prints a short line per episode, and says where
pending proposals wait. A project error (an unreachable source or exchange) is
printed as one line, never a traceback: inside a pass it is a note and the loop
goes on; outside one the command exits 1. Imports happen inside the commands.
"""

from __future__ import annotations

import typer

from pmfa.records.base import RunMode

DEFAULT_INTERVAL_SECONDS = 300.0

SHADOW_BANNER = (
    "pmfa shadow: live production market data from Kalshi (read-only, unsigned: no key is "
    "loaded), live Federal Reserve and BLS listings, live models.\n"
    "Fills are simulated against the live order book and recorded in the local state database. "
    "No order is ever sent to any exchange. Mock funds only; not trading advice."
)
SHADOW_REVIEW = "Approve-tier proposals pause for your review (--review)."
SHADOW_TAGGED = (
    "Approve-tier proposals are filled on paper and tagged (pass --review to decide them yourself)."
)
DEMO_BANNER = (
    "pmfa demo: Kalshi's demo exchange ({url}) for market data and orders, mock funds only, "
    "subaccount {sub}.\n"
    "Every order needs your approval: proposals pause until you run "
    "pmfa review approve|resize|reject --mode demo. Nothing is sent to the production exchange.\n"
    "Demo prices may not reflect real markets; demo P&L is never reported as evidence."
)


def _choose(once: bool, loop: bool) -> bool:
    """True for a loop. ``--once`` is the default; both together is an error."""
    if once and loop:
        raise typer.BadParameter("choose one of --once and --loop", param_hint="--loop")
    return loop


def _run(mode: RunMode, *, loop: bool, interval: float, review: bool) -> None:
    from pmfa.config import load_config
    from pmfa.errors import ConfigError, PmfaError
    from pmfa.live import session as live_session
    from pmfa.live.loop import PassSummary, run_loop
    from pmfa.live.text import pass_lines

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(2) from None
    if mode is RunMode.SHADOW:
        typer.echo(SHADOW_BANNER)
        typer.echo(SHADOW_REVIEW if review else SHADOW_TAGGED)
    else:
        typer.echo(
            DEMO_BANNER.format(url=config.kalshi.demo_base_url, sub=config.kalshi.demo_subaccount)
        )
    typer.echo(
        f"{'Looping every ' + format(interval, 'g') + ' s until Ctrl-C' if loop else 'One pass'}."
    )
    try:
        with live_session.open_session(mode, config, review=review) as s:

            def show(summary: PassSummary) -> None:
                for line in pass_lines(summary):
                    typer.echo(line)
                if summary.pending_review:
                    typer.echo(f"{len(summary.pending_review)} proposal(s) wait for review.")

            try:
                run_loop(
                    s.world,
                    s.controller,
                    interval_seconds=interval,
                    max_passes=None if loop else 1,
                    on_pass=show,
                )
            except KeyboardInterrupt:
                typer.echo("stopped by you")
            typer.echo(
                f"Pending proposals wait in {s.state_db} (pmfa review list --mode {mode.value})."
            )
    except ConfigError as exc:
        typer.echo(f"cannot start {mode.value} mode: {exc}", err=True)
        raise typer.Exit(2) from None
    except PmfaError as exc:
        # A source or the exchange failed outside a pass (a failed pass is a note instead).
        typer.echo(f"{mode.value} mode stopped: {exc}", err=True)
        raise typer.Exit(1) from None


def shadow(
    once: bool = typer.Option(False, "--once", help="Run one pass and exit (the default)."),
    loop: bool = typer.Option(False, "--loop", help="Repeat passes until Ctrl-C."),
    interval: float = typer.Option(
        DEFAULT_INTERVAL_SECONDS, "--interval", min=1.0, help="Seconds between passes with --loop."
    ),
    review: bool = typer.Option(
        False, "--review", help="Pause approve-tier proposals for your review."
    ),
) -> None:
    """Shadow mode: live data, simulated fills, no orders."""
    _run(RunMode.SHADOW, loop=_choose(once, loop), interval=interval, review=review)


def demo(
    once: bool = typer.Option(False, "--once", help="Run one pass and exit (the default)."),
    loop: bool = typer.Option(False, "--loop", help="Repeat passes until Ctrl-C."),
    interval: float = typer.Option(
        DEFAULT_INTERVAL_SECONDS, "--interval", min=1.0, help="Seconds between passes with --loop."
    ),
) -> None:
    """Demo mode: Kalshi's demo exchange, mock funds, every order needs your approval."""
    _run(RunMode.DEMO, loop=_choose(once, loop), interval=interval, review=True)


def register(app: typer.Typer) -> None:
    app.command("shadow")(shadow)
    app.command("demo")(demo)


__all__ = ["demo", "register", "shadow"]
