"""``pmfa review``, ``pmfa halt`` and ``pmfa stage``: human oversight of the live modes.

- ``review list | show ID | approve ID | resize ID --contracts N | reject ID --reason CODE``.
  Deciding resumes the paused episode through a controller built on the same
  state and checkpoint files as the process that paused it, and prints what
  happened (executed with fills, gate rejected at the current price, expired, ...).
- ``halt [--reason TEXT]`` sets the persisted flag, then cancels every open
  order; ``halt --resume`` clears it; ``halt --status`` shows it.
- ``stage status | advance | demote --reason TEXT`` through the stage manager.

Every command takes ``--mode shadow|demo`` (default shadow) to choose the
world. Commands that only read or write stored state (review list and show,
halt status and resume, stage) open the mode's database alone, so they work
without network access or demo credentials. Imports happen inside the commands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from pmfa.live.session import LiveMode

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.execution.stages import StageManager
    from pmfa.records.base import Record
    from pmfa.records.decision import ReasonCode, ReviewDecision

review_app = typer.Typer(
    name="review", help="List, show and decide proposals that wait for you.", no_args_is_help=True
)
stage_app = typer.Typer(
    name="stage", help="Show and move the autonomy stage.", no_args_is_help=True
)

MODE_OPTION = typer.Option(LiveMode.SHADOW, "--mode", help="Which live mode's world.")
REASON_CODES = "stale_evidence, wrong_market, size_too_large, disagree_with_forecast, other"


def _config() -> Config:
    from pmfa.config import load_config
    from pmfa.errors import ConfigError

    try:
        return load_config()
    except ConfigError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        raise typer.Exit(2) from None


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


# --------------------------------------------------------------------------- review


@review_app.command("list")
def review_list(mode: LiveMode = MODE_OPTION) -> None:
    """List the proposals that wait for a decision."""
    from datetime import UTC, datetime

    from pmfa.live.session import open_store
    from pmfa.live.text import pending_line
    from pmfa.records.decision import ProposalStatus

    config = _config()
    with open_store(mode.run_mode, config) as store:
        pending = store.list_proposals(ProposalStatus.PENDING)
    if not pending:
        typer.echo(f"no pending proposals ({mode.value})")
        return
    now = datetime.now(UTC)
    for proposal, status in pending:
        typer.echo(pending_line(proposal, status, now))


@review_app.command("show")
def review_show(
    proposal_id: str = typer.Argument(..., help="The proposal id."), mode: LiveMode = MODE_OPTION
) -> None:
    """Show a proposal's review card."""
    from pmfa.live.session import open_store
    from pmfa.live.text import card_lines, proposal_line

    config = _config()
    with open_store(mode.run_mode, config) as store:
        found = store.get_proposal(proposal_id)
    if found is None:
        raise _fail(f"no proposal {proposal_id!r} ({mode.value})")
    proposal, status, card = found
    typer.echo(f"status:   {status.value}")
    if card is None:
        typer.echo(proposal_line(proposal))
        return
    for line in card_lines(card):
        typer.echo(line)


def _decide(
    mode: LiveMode,
    proposal_id: str,
    decision: ReviewDecision,
    *,
    contracts: int | None = None,
    reason_code: ReasonCode | None = None,
    note: str = "",
) -> None:
    from pmfa.episode import NotPaused
    from pmfa.errors import ConfigError, NotFound
    from pmfa.execution.review import ReviewRefused
    from pmfa.live import session as live_session
    from pmfa.live.text import decision_lines
    from pmfa.records.decision import Review

    config = _config()
    try:
        with live_session.open_session(mode.run_mode, config, review=True) as s:
            found = s.world.store.get_proposal(proposal_id)
            if found is None:
                raise _fail(f"no proposal {proposal_id!r} ({mode.value})")
            proposal = found[0]
            review = Review(
                proposal_id=proposal_id,
                decision=decision,
                contracts=contracts,
                reason_code=reason_code,
                note=note,
                decided_at=s.world.clock.now(),
            )
            try:
                record = s.controller.resume(proposal.episode_id, review)
            except (ReviewRefused, NotPaused, NotFound, ValueError) as exc:
                raise _fail(f"not decided: {exc}") from None
    except ConfigError as exc:
        raise _fail(f"cannot open {mode.value} mode: {exc}", 2) from None
    typer.echo(f"{decision.value} {proposal_id} -> episode {record.episode_id}")
    for line in decision_lines(record):
        typer.echo(line)


@review_app.command("approve")
def review_approve(
    proposal_id: str = typer.Argument(..., help="The proposal id."), mode: LiveMode = MODE_OPTION
) -> None:
    """Approve a proposal: the gate re-runs at the current price, then the order is placed."""
    from pmfa.records.decision import ReviewDecision

    _decide(mode, proposal_id, ReviewDecision.APPROVE)


def _reason(code: str | None) -> ReasonCode | None:
    from pmfa.records.decision import ReasonCode

    if code is None:
        return None
    try:
        return ReasonCode(code)
    except ValueError:
        raise typer.BadParameter(
            f"{code!r} is not a reason code ({REASON_CODES})", param_hint="--reason"
        ) from None


@review_app.command("resize")
def review_resize(
    proposal_id: str = typer.Argument(..., help="The proposal id."),
    contracts: int = typer.Option(..., "--contracts", min=1, help="The new, smaller size."),
    reason: str | None = typer.Option(None, "--reason", help=f"Reason code: {REASON_CODES}."),
    mode: LiveMode = MODE_OPTION,
) -> None:
    """Shrink a proposal and approve the smaller order."""
    from pmfa.records.decision import ReviewDecision

    _decide(
        mode, proposal_id, ReviewDecision.RESIZE, contracts=contracts, reason_code=_reason(reason)
    )


@review_app.command("reject")
def review_reject(
    proposal_id: str = typer.Argument(..., help="The proposal id."),
    reason: str = typer.Option(..., "--reason", help=f"Reason code: {REASON_CODES}."),
    note: str = typer.Option("", "--note", help="A free-text note kept with the review."),
    mode: LiveMode = MODE_OPTION,
) -> None:
    """Reject a proposal with a reason code: nothing is placed."""
    from pmfa.records.decision import ReviewDecision

    _decide(mode, proposal_id, ReviewDecision.REJECT, reason_code=_reason(reason), note=note)


# --------------------------------------------------------------------------- halt


def halt(
    reason: str = typer.Option("manual halt", "--reason", help="Why you are halting."),
    resume: bool = typer.Option(False, "--resume", help="Clear the halt flag."),
    status: bool = typer.Option(False, "--status", help="Show the halt flag and exit."),
    mode: LiveMode = MODE_OPTION,
) -> None:
    """Halt: set the persisted flag (checked before every order) and cancel all open orders."""
    from datetime import UTC, datetime

    from pmfa.errors import ConfigError, ExchangeError
    from pmfa.execution.halt import halt as halt_now
    from pmfa.live import session as live_session
    from pmfa.live.session import open_store
    from pmfa.live.text import when

    if resume and status:
        raise typer.BadParameter("choose one of --resume and --status", param_hint="--resume")
    config = _config()
    if status or resume:
        with open_store(mode.run_mode, config) as store:
            if resume:
                store.clear_halt()
                typer.echo(f"halt cleared ({mode.value}); new orders are allowed again")
                return
            state = store.get_halt()
        if state.halted:
            typer.echo(f"HALTED ({mode.value}) since {when(state.at)}: {state.reason}")
        else:
            typer.echo(f"not halted ({mode.value})")
        return

    # The flag is set first, from the database alone, so a halt never depends on the exchange.
    with open_store(mode.run_mode, config) as store:
        store.set_halt(reason, datetime.now(UTC))
    typer.echo(f"halt flag set ({mode.value}): {reason}")
    try:
        with live_session.open_session(mode.run_mode, config) as s:
            cancelled = halt_now(s.world.store, s.world.executor, s.world.clock, reason)
    except (ConfigError, ExchangeError) as exc:
        raise _fail(f"the flag is set, but open orders could not be cancelled: {exc}") from None
    typer.echo(f"cancelled {cancelled} open order(s); no new order will be placed until --resume")


# --------------------------------------------------------------------------- stage


def _benchmark_lines(name: str, benchmark: Record, *, gates: bool) -> list[str]:
    data = benchmark.model_dump(mode="json")
    met = "met" if data.pop("met") else "not met"
    shortfalls = data.pop("shortfalls", [])
    lines = [f"{name}: {met}" + ("  <- gates the next stage" if gates else "")]
    lines.extend(f"  {k}: {v}" for k, v in data.items())
    lines.extend(f"  short: {s}" for s in shortfalls)
    return lines


def _stage_report(manager: StageManager) -> list[str]:
    from pmfa.evaluation.benchmarks import (
        approval_benchmark,
        backtest_benchmark,
        shadow_benchmark,
    )
    from pmfa.execution.stages import next_stage

    config = manager.config
    state = manager.store.get_stage()
    nxt = next_stage(state.stage)
    lines = [
        f"stage: {state.stage.value}"
        + (f" ({state.reason})" if state.reason else "")
        + (f"; next: {nxt.value}" if nxt else "; top of the ladder")
    ]
    trades, start = manager.shadow_trades()
    now = manager.clock.now()
    benchmarks = [
        ("backtest -> shadow", backtest_benchmark(manager.forecast_rows(), stages=config.stages)),
        (
            "shadow -> approval",
            shadow_benchmark(
                trades,
                bankroll=config.execution.notional_bankroll,
                start=start or now,
                end=now,
                stages=config.stages,
            ),
        ),
        (
            "approval -> autonomous",
            approval_benchmark(manager.approval_reviews(state), stages=config.stages),
        ),
    ]
    gate = f"{state.stage.value} -> {nxt.value}" if nxt else None
    for name, benchmark in benchmarks:
        lines.extend(_benchmark_lines(name, benchmark, gates=name == gate))
    return lines


@stage_app.command("status")
def stage_status(mode: LiveMode = MODE_OPTION) -> None:
    """Show the stage and every benchmark with its numbers."""
    from pmfa.execution.stages import StageManager
    from pmfa.live.session import open_store
    from pmfa.world.clocks import SystemClock

    config = _config()
    with open_store(mode.run_mode, config) as store:
        for line in _stage_report(StageManager(store, config, SystemClock())):
            typer.echo(line)


@stage_app.command("advance")
def stage_advance(mode: LiveMode = MODE_OPTION) -> None:
    """Move up one stage, only if the benchmark for it is met."""
    from pmfa.execution.stages import StageManager, StageNotMet
    from pmfa.live.session import open_store
    from pmfa.world.clocks import SystemClock

    config = _config()
    with open_store(mode.run_mode, config) as store:
        manager = StageManager(store, config, SystemClock())
        try:
            state = manager.advance()
        except StageNotMet as exc:
            for line in _stage_report(manager):
                typer.echo(line)
            raise _fail(f"not advanced: {exc}") from None
    typer.echo(f"advanced to {state.stage.value}: {state.reason}")


@stage_app.command("demote")
def stage_demote(
    reason: str = typer.Option(..., "--reason", help="Why the stage drops."),
    mode: LiveMode = MODE_OPTION,
) -> None:
    """Drop one stage and record why."""
    from pmfa.execution.stages import StageManager
    from pmfa.live.session import open_store
    from pmfa.world.clocks import SystemClock

    config = _config()
    with open_store(mode.run_mode, config) as store:
        state = StageManager(store, config, SystemClock()).demote(reason)
    typer.echo(f"demoted to {state.stage.value}: {state.reason}")


def register(app: typer.Typer) -> None:
    app.add_typer(review_app, name="review")
    app.command("halt")(halt)
    app.add_typer(stage_app, name="stage")


__all__ = ["halt", "register", "review_app", "stage_app"]
