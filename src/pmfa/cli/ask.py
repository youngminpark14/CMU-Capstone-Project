"""``pmfa ask SERIES``: one shadow episode now, on one market, and your decision on its proposal.

The steps and what each prints are in ``pmfa.live.ask``. This module parses the
options, says what the run will and will not do, prompts for the decision on the
terminal (or applies ``--decide``), and exits with the run's code: 0 when the
episode ended in a named exit other than ``error``, 1 when it could not run or
ended in ``error``, 2 for a configuration problem or a prompt with no terminal.
Imports happen inside the command.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pmfa.live.ask import Choice
    from pmfa.records.decision import ReviewCard

ASK_BANNER = (
    "pmfa ask: one shadow-mode episode on one market, now. It reads live Kalshi market data "
    "(read-only, unsigned: no key is loaded), live evidence and live models.\n"
    "A fill is simulated on paper against the live order book; nothing is ever sent to any "
    "exchange. Mock funds only; not trading advice.\n"
    "Any proposal waits for your decision; no answer (Ctrl-C or Ctrl-D at the question) "
    "rejects it."
)


class DecideMode(StrEnum):
    """How the proposal is decided: prompt on the terminal, or decide it in advance."""

    ASK = "ask"
    APPROVE = "approve"
    REJECT = "reject"


DECIDE_OPTION = typer.Option(
    DecideMode.ASK, "--decide", help="ask: prompt on the terminal; approve or reject: decide now."
)


def interactive() -> bool:
    """True when standard input is a terminal a person can answer on."""
    return sys.stdin.isatty()


def _prompt(card: ReviewCard) -> Choice:
    """Ask on the terminal until the answer is approve, reject, or resize to 1 .. contracts - 1.

    Ctrl-C or Ctrl-D at the prompt rejects: the proposal is decided in the run, since
    ``pmfa review`` could not approve it later (see ``pmfa.live.ask.Decide``).
    """
    from pmfa.records.decision import ReviewDecision

    most = card.contracts - 1
    while True:
        try:
            answer = typer.prompt(
                f"Approve, reject, or resize to N contracts (fewer than {card.contracts})? "
                "[approve / reject / resize N]"
            )
        except (EOFError, typer.Abort):
            typer.echo("\nNo answer: rejecting.")
            return ReviewDecision.REJECT, None
        words = answer.strip().lower().split()
        if words in (["approve"], ["a"]):
            return ReviewDecision.APPROVE, None
        if words in (["reject"], ["r"]):
            return ReviewDecision.REJECT, None
        if len(words) == 2 and words[0] == "resize" and words[1].isdigit():
            if 1 <= int(words[1]) <= most:
                return ReviewDecision.RESIZE, int(words[1])
            typer.echo(
                f"A resize takes 1 to {most} contracts."
                if most >= 1
                else "1 contract cannot be made smaller: approve or reject."
            )
            continue
        typer.echo("Type approve, reject, or resize followed by a number of contracts.")


def ask(
    series: str = typer.Argument(..., help="A configured series ticker, such as KXBTCD."),
    market: str | None = typer.Option(
        None, "--market", help="Ask about this market (it must be open), not the chosen one."
    ),
    decide: DecideMode = DECIDE_OPTION,
    min_minutes: int = typer.Option(
        10, "--min-minutes", min=0, help="Pass over an event closing sooner than this."
    ),
) -> None:
    """Run one live episode on one market now and decide its proposal yourself (shadow mode)."""
    if decide is DecideMode.ASK and not interactive():
        typer.echo(
            "pmfa ask: standard input is not a terminal, so you cannot be asked to decide. "
            "Pass --decide approve or --decide reject.",
            err=True,
        )
        raise typer.Exit(2)

    from pmfa.config import load_config
    from pmfa.errors import ConfigError
    from pmfa.live.ask import asked_config, run_ask
    from pmfa.records.decision import ReviewDecision

    series = series.upper()
    market = market.upper() if market else None
    try:
        config = load_config()
        asked_config(config, series)
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(2) from None

    def choose(card: ReviewCard) -> Choice:
        if decide is DecideMode.APPROVE:
            typer.echo("--decide approve: approving.")
            return ReviewDecision.APPROVE, None
        if decide is DecideMode.REJECT:
            typer.echo("--decide reject: rejecting.")
            return ReviewDecision.REJECT, None
        return _prompt(card)

    typer.echo(ASK_BANNER)
    try:
        result = run_ask(
            config,
            series,
            decide=choose,
            say=typer.echo,
            market_ticker=market,
            min_minutes=min_minutes,
        )
    except ConfigError as exc:
        typer.echo(f"cannot start shadow mode: {exc}", err=True)
        raise typer.Exit(2) from None
    raise typer.Exit(result.exit_code)


def register(app: typer.Typer) -> None:
    app.command("ask")(ask)


__all__ = ["ask", "register"]
