"""``pmfa backtest`` and ``pmfa report``: run or plan a backtest, then build and publish its report.

``pmfa backtest`` writes a run directory under ``config.paths.runs_dir``
(``runs/``, ignored by git) named ``<date>-<run name>``. ``--dry-run`` makes no
model call and prints the plan with its dollar estimate. Without an Anthropic
API key, the systems that need a model are recorded as not run and only the
market is scored. ``pmfa report RUN_DIR`` builds ``report.md`` and its figures;
``--publish NAME`` copies the committed artifact set to ``evaluation/runs/NAME``.

Imports happen inside the commands, so ``pmfa --help`` stays fast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from pmfa.evaluation.manifest import RunManifest


def _print_plan(manifest: RunManifest) -> None:
    from decimal import Decimal

    plan = manifest.plan
    typer.echo(f"Run directory: {manifest.name} ({manifest.status})")
    typer.echo(
        f"Window: {manifest.window_start.isoformat()} to {manifest.window_end.isoformat()}; "
        f"{plan.scoring_points} scoring point(s), {plan.triggers} trading trigger(s)"
        + (f" (limit {plan.limit})" if plan.limit else "")
    )
    if plan.left_out_points or plan.left_out_triggers:
        typer.echo(
            f"Left out by the limit: {len(plan.left_out_points)} point(s), "
            f"{len(plan.left_out_triggers)} trigger(s) (listed in the manifest)"
        )
    typer.echo(f"Models: {manifest.models_source}")
    typer.echo("")
    typer.echo(
        f"{'system':<24} {'status':<22} {'forecasts':>9} {'calls (min-max)':>17} {'USD (min-max)':>19}"
    )
    for s in plan.systems:
        typer.echo(
            f"{s.system:<24} {s.status:<22} {s.forecasts:>9} "
            f"{s.model_calls_min:>7}-{s.model_calls_max:<9} "
            f"{float(s.usd_min):>8.2f}-{float(s.usd_max):<10.2f}"
        )
        for note in s.notes:
            typer.echo(f"{'':<26}{note}")
    typer.echo("")
    cap = Decimal(str(manifest.config["budget"]["max_usd_per_run"]))
    typer.echo(
        f"Estimated total: ${float(plan.usd_min):.2f} to ${float(plan.usd_max):.2f} "
        f"(run cap ${cap})"
    )
    if plan.usd_max > cap:
        typer.echo(
            f"The maximum exceeds the run's dollar cap of ${cap}: if the run spends that much "
            "it stops at the cap and is marked partial, with what it did not run listed."
        )
    typer.echo("Assumptions:")
    for line in plan.assumptions:
        typer.echo(f"  - {line}")


def register(app: typer.Typer) -> None:
    @app.command("backtest")
    def backtest(
        systems: str | None = typer.Option(
            None,
            "--systems",
            help="Comma-separated: market, prompt_only, single_pass, single_pass_similarity, "
            "pipeline. Default: evaluation.systems from the configuration.",
        ),
        no_score: bool = typer.Option(False, "--no-score", help="Skip the scoring pass."),
        no_trade: bool = typer.Option(False, "--no-trade", help="Skip the trading pass."),
        limit: int | None = typer.Option(
            None, "--limit", min=1, help="Evenly spaced subsample of N points and N triggers."
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Print the plan and a dollar estimate; call no model."
        ),
        cap: float | None = typer.Option(
            None, "--cap", help="Dollar cap for the run (budget.max_usd_per_run)."
        ),
        run_name: str | None = typer.Option(
            None, "--run-name", help="Name of the run directory, after the date."
        ),
        config_path: Annotated[
            Path | None, typer.Option("--config", help="Configuration file.")
        ] = None,
    ) -> None:
        """Score the systems on the frozen snapshot and simulate news-triggered trading."""
        from decimal import Decimal

        from pmfa.config import load_config
        from pmfa.errors import PmfaError
        from pmfa.evaluation.backtest import run_backtest

        overrides = {}
        if cap is not None:
            usd = Decimal(str(cap))
            overrides["budget"] = {"max_usd_per_run": str(usd)}
        config = load_config(config_path, overrides or None)
        if cap is not None and config.budget.max_usd_per_episode > config.budget.max_usd_per_run:
            config = load_config(
                config_path,
                {"budget": {"max_usd_per_run": str(usd), "max_usd_per_episode": str(usd)}},
            )
        now = datetime.now(UTC)
        name = f"{now:%Y-%m-%d}-{run_name}" if run_name else f"{now:%Y-%m-%d-%H%M%S}"
        run_dir = Path(config.paths.runs_dir) / name
        chosen = [s.strip() for s in systems.split(",") if s.strip()] if systems else None
        try:
            manifest = run_backtest(
                config,
                run_dir=run_dir,
                systems=chosen,
                score=not no_score,
                trade=not no_trade,
                limit=limit,
                dry_run=dry_run,
            )
        except (PmfaError, ValueError) as exc:
            typer.echo(f"backtest refused: {exc}", err=True)
            raise typer.Exit(1) from exc
        if dry_run:
            _print_plan(manifest)
            typer.echo(f"\nPlan written to {run_dir / 'manifest.json'}")
            return
        typer.echo(f"Run {manifest.status}: {run_dir}")
        for system, status in manifest.systems.items():
            typer.echo(f"  {system}: {status}")
        typer.echo(
            f"Spent ${float(manifest.usage.cost_usd):.4f} on {manifest.usage.live_answers} new "
            f"answer(s); {manifest.usage.cache_answers} from the cache."
        )
        typer.echo(f"Next: pmfa report {run_dir}")

    @app.command("report")
    def report(
        run_dir: Annotated[Path, typer.Argument(help="A run directory written by pmfa backtest.")],
        publish: str | None = typer.Option(
            None, "--publish", help="Copy the committed artifact set to evaluation/runs/NAME."
        ),
        scores_only: bool = typer.Option(
            False,
            "--scores-only",
            help="With --publish: leave out the per-forecast table and the traces, "
            "which hold exchange prices.",
        ),
    ) -> None:
        """Build report.md and figures/ for a run, and optionally publish it."""
        from pmfa.evaluation.report import build_report, publish_run

        if not (run_dir / "manifest.json").exists():
            typer.echo(f"{run_dir} has no manifest.json: not a run directory", err=True)
            raise typer.Exit(1)
        path = build_report(run_dir)
        typer.echo(f"Report written to {path}")
        if publish:
            from pmfa.evaluation.manifest import read_manifest
            from pmfa.evaluation.report import dirty_notice

            try:
                target = publish_run(run_dir, publish, scores_only=scores_only)
            except ValueError as exc:
                typer.echo(f"not published: {exc}", err=True)
                raise typer.Exit(1) from exc
            typer.echo(f"Published to {target}")
            manifest = read_manifest(run_dir)
            if manifest.git_dirty:
                typer.echo(f"Warning: {dirty_notice(manifest)}", err=True)


__all__ = ["register"]
