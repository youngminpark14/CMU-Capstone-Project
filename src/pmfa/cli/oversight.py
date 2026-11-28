"""``pmfa review``, ``pmfa reconcile``, ``pmfa halt`` and ``pmfa stage``: oversight of the live modes.

- ``review list | show ID | approve ID | resize ID --contracts N | reject ID --reason CODE``.
  Deciding resumes the paused episode through a controller built on the same
  state and checkpoint files as the process that paused it, and prints what
  happened (executed with fills, gate rejected at the current price, expired, ...).
- ``reconcile [--episode ID]`` asks the exchange now about every order whose
  outcome is unknown (an exchange error while sending, and no lookup could find
  it), or about the one named episode whose order may be on the exchange. It
  looks each order up by its client order id and never sends one, halted or not,
  and prints one line per episode; it exits 1 while any outcome is still unknown.
- ``halt [--reason TEXT]`` sets the persisted flag, then cancels every open
  order; ``halt --resume`` clears it; ``halt --status`` shows it. Without
  ``--mode``, halt acts on every live mode (shadow and demo): all the flags are
  set before any exchange is contacted, and a mode whose world cannot be opened
  (demo without its credentials) is reported while its flag stays set.
- ``stage status | advance | demote --reason TEXT`` through the stage manager.
  ``status`` and ``advance`` take ``--from-run RUN_DIR`` (repeatable): the
  backtest -> shadow benchmark then also counts the scored forecasts of those
  ``pmfa backtest`` runs, whose ledgers live in the run directories, not in the
  live database. The ladder governs real-money progression only.

Every other command takes ``--mode shadow|demo`` (default shadow) to choose the
world; for halt, ``--mode`` narrows it to one mode. Commands that only read or
write stored state (review list and show, halt status and resume, stage) open
the mode's database alone, so they work without network access or demo
credentials. Imports happen inside the commands.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from pmfa.live.session import LiveMode

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.execution.stages import StageManager
    from pmfa.ports import StateStore as StateStoreType
    from pmfa.records.base import Record
    from pmfa.records.decision import ReasonCode, ReviewDecision

review_app = typer.Typer(
    name="review", help="List, show and decide proposals that wait for you.", no_args_is_help=True
)
stage_app = typer.Typer(
    name="stage", help="Show and move the autonomy stage.", no_args_is_help=True
)

MODE_OPTION = typer.Option(LiveMode.SHADOW, "--mode", help="Which live mode's world.")
HALT_MODE_OPTION = typer.Option(
    None, "--mode", help="Only this live mode (default: every live mode)."
)
FROM_RUN_OPTION = typer.Option(
    None,
    "--from-run",
    help="A pmfa backtest run directory whose scored forecasts count toward "
    "backtest -> shadow (repeatable).",
)
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
    from pmfa.errors import ConfigError
    from pmfa.live import session as live_session
    from pmfa.live.text import decision_lines

    config = _config()
    try:
        with live_session.open_session(mode.run_mode, config, review=True) as s:
            try:
                record = live_session.decide(
                    s,
                    proposal_id,
                    decision,
                    contracts=contracts,
                    reason_code=reason_code,
                    note=note,
                )
            except live_session.NotDecided as exc:
                raise _fail(str(exc)) from None
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


# --------------------------------------------------------------------------- reconcile


def reconcile(
    mode: LiveMode = MODE_OPTION,
    episode_id: str | None = typer.Option(
        None, "--episode", help="Only this episode, also one that died while sending its order."
    ),
) -> None:
    """Ask the exchange now about every order whose outcome is unknown. Sends nothing.

    One line per such order: what the exchange said, or that it still cannot say.
    Exits 0 when no outcome remains unknown, 1 when one does.
    """
    from pmfa.errors import ConfigError, PmfaError
    from pmfa.live import session as live_session
    from pmfa.live.text import unknown_outcome_sentence
    from pmfa.records.episode import EpisodeStatus

    config = _config()
    remaining = 0
    failed = 0
    try:
        with live_session.open_session(mode.run_mode, config) as s:
            waiting = (
                [episode_id]
                if episode_id
                else [
                    e.episode_id
                    for e in s.world.store.list_episodes(
                        status=EpisodeStatus.OUTCOME_UNKNOWN, limit=1_000_000
                    )
                ]
            )
            if episode_id and s.world.store.get_episode(episode_id) is None:
                raise _fail(f"no episode {episode_id!r} ({mode.value})")
            if not waiting:
                typer.echo(f"no order with an unknown outcome ({mode.value})")
                return
            for eid in waiting:
                stored = s.world.store.get_episode(eid)
                who = (
                    f"proposal {stored.proposal.proposal_id}"
                    if stored and stored.proposal
                    else f"episode {eid}"
                )
                try:
                    record = s.controller.reconcile(eid)
                except PmfaError as exc:
                    typer.echo(f"{who}: still unknown, the reconcile failed: {exc}")
                    failed += 1
                    continue
                if record.status is EpisodeStatus.OUTCOME_UNKNOWN:
                    last = record.trace[-1] if record.trace else None
                    reason = str(last.data.get("detail", "")) if last is not None else ""
                    typer.echo(f"{who}: still unknown" + (f": {reason}" if reason else ""))
                    remaining += 1
                    continue
                end = record.exit.value if record.exit is not None else record.status.value
                typer.echo(f"{who}: {end}" + (f": {record.exit_detail}" if record.exit else ""))
    except ConfigError as exc:
        raise _fail(f"cannot open {mode.value} mode: {exc}", 2) from None
    if remaining:
        typer.echo(
            f"{remaining} order outcome(s) still unknown. "
            + unknown_outcome_sentence(mode.run_mode)
        )
    if remaining or failed:
        raise typer.Exit(1)
    typer.echo(f"no order outcome remains unknown ({mode.value})")


# --------------------------------------------------------------------------- halt


def halt(
    reason: str = typer.Option("manual halt", "--reason", help="Why you are halting."),
    resume: bool = typer.Option(False, "--resume", help="Clear the halt flag."),
    status: bool = typer.Option(False, "--status", help="Show the halt flag and exit."),
    mode: LiveMode | None = HALT_MODE_OPTION,
) -> None:
    """Halt: set the persisted flag (checked before every order) and cancel all open orders.

    Without ``--mode`` this acts on every live mode: the flag is set in each
    mode's state database before any exchange is contacted, then each mode's
    open orders are cancelled. ``--resume`` and ``--status`` follow the same rule.
    """
    from datetime import UTC, datetime

    from pmfa.errors import ConfigError, ExchangeError
    from pmfa.execution.halt import halt as halt_now
    from pmfa.live import session as live_session
    from pmfa.live.session import open_store
    from pmfa.live.text import when

    if resume and status:
        raise typer.BadParameter("choose one of --resume and --status", param_hint="--resume")
    config = _config()
    modes = [mode] if mode is not None else list(LiveMode)
    if status or resume:
        for m in modes:
            with open_store(m.run_mode, config) as store:
                if resume:
                    store.clear_halt()
                    typer.echo(f"halt cleared ({m.value}); new orders are allowed again")
                    continue
                state = store.get_halt()
            if state.halted:
                typer.echo(f"HALTED ({m.value}) since {when(state.at)}: {state.reason}")
            else:
                typer.echo(f"not halted ({m.value})")
        return

    # Every flag is set first, from the databases alone, so a halt never depends on an exchange.
    at = datetime.now(UTC)
    for m in modes:
        with open_store(m.run_mode, config) as store:
            store.set_halt(reason, at)
        typer.echo(f"halt flag set ({m.value}): {reason}")
    failed = False
    for m in modes:
        try:
            with live_session.open_session(m.run_mode, config) as s:
                cancelled = halt_now(s.world.store, s.world.executor, s.world.clock, reason)
        except ConfigError as exc:
            # Without --mode, a mode that cannot be opened here (demo without its keys) is
            # reported; its flag still blocks every new order.
            typer.echo(
                f"{m.value}: the flag is set; open orders were not cancelled: {exc}", err=True
            )
            failed = failed or mode is not None
            continue
        except ExchangeError as exc:
            typer.echo(
                f"{m.value}: the flag is set, but open orders could not be cancelled: {exc}",
                err=True,
            )
            failed = True
            continue
        typer.echo(f"{m.value}: cancelled {cancelled} open order(s)")
    names = ", ".join(m.value for m in modes)
    typer.echo(f"no new order will be placed in {names} until pmfa halt --resume")
    if failed:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- stage


def _benchmark_lines(name: str, benchmark: Record, *, gates: bool) -> list[str]:
    data = benchmark.model_dump(mode="json")
    met = "met" if data.pop("met") else "not met"
    shortfalls = data.pop("shortfalls", [])
    lines = [f"{name}: {met}" + ("  <- gates the next stage" if gates else "")]
    lines.extend(f"  {k}: {v}" for k, v in data.items())
    lines.extend(f"  short: {s}" for s in shortfalls)
    return lines


def _manager(store: StateStoreType, config: Config, runs: list[Path] | None) -> StageManager:
    """A stage manager over ``store`` that also counts the backtested forecasts of ``runs``."""
    from pmfa.errors import NotFound
    from pmfa.execution.stages import StageManager, run_forecast_rows
    from pmfa.world.clocks import SystemClock

    rows = []
    for run_dir in runs or []:
        try:
            rows.extend(run_forecast_rows(run_dir))
        except NotFound as exc:
            raise _fail(f"--from-run: {exc}") from None
    return StageManager(store, config, SystemClock(), backtest_rows=rows)


def _stage_report(manager: StageManager, runs: list[Path] | None = None) -> list[str]:
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
        if name == "backtest -> shadow" and runs:
            lines.append("  backtest forecasts from: " + ", ".join(str(r) for r in runs))
    return lines


@stage_app.command("status")
def stage_status(
    mode: LiveMode = MODE_OPTION, from_run: list[Path] | None = FROM_RUN_OPTION
) -> None:
    """Show the stage and every benchmark with its numbers.

    ``pmfa backtest`` keeps its ledger in the run directory; name runs with
    ``--from-run`` to count their scored forecasts toward backtest -> shadow.
    """
    from pmfa.live.session import open_store

    config = _config()
    with open_store(mode.run_mode, config) as store:
        for line in _stage_report(_manager(store, config, from_run), from_run):
            typer.echo(line)


@stage_app.command("advance")
def stage_advance(
    mode: LiveMode = MODE_OPTION, from_run: list[Path] | None = FROM_RUN_OPTION
) -> None:
    """Move up one stage, only if the benchmark for it is met."""
    from pmfa.execution.stages import StageNotMet
    from pmfa.live.session import open_store

    config = _config()
    with open_store(mode.run_mode, config) as store:
        manager = _manager(store, config, from_run)
        try:
            state = manager.advance()
        except StageNotMet as exc:
            for line in _stage_report(manager, from_run):
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
    app.command("reconcile")(reconcile)
    app.command("halt")(halt)
    app.add_typer(stage_app, name="stage")


__all__ = ["halt", "reconcile", "register", "review_app", "stage_app"]
