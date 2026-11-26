"""Rehearse the whole backtest on the frozen data, with no key and no spending.

Every role answer here comes from a fixed rule in this file, not from a model:
the Monitor takes the first candidate event, the Researcher keeps every passage,
the Generator proposes one scenario, and the Critic and the baselines always
give the same distribution (most of the mass on the middle option, or quantiles
around the middle strike). **So no forecast in a rehearsal means anything, and
nothing it scores is a result.** Its manifest and report say the models were
supplied by the caller.

What a rehearsal is for: it drives every step of the real pipeline over the real
snapshot and corpus before any money is spent. It builds the retrieval indexes,
runs every scoring point for every system and every news trigger of the trading
pass, sizes and gates orders against real books, fills them from real candles,
applies the stops, saves traces and replays each one, and writes the report. If
a rehearsal completes and its traces replay, a paid run will not fail on the
data or the plumbing; what it cannot show is how a model's answers behave.

Run it after ``pmfa data build``, from the repository root::

    uv run python scripts/rehearse_backtest.py

It writes ``runs/<date>-rehearsal`` (ignored by git), prints a summary, and
exits 0 only when the run completed and every saved trace replays and matches.
Do not publish a rehearsal: it is a test of the harness.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from pmfa.config import Config, load_config
from pmfa.models import ScriptedModels
from pmfa.records.base import Record
from pmfa.records.roles import (
    BaselineForecast,
    BaselineInput,
    EventBrief,
    GenerateInput,
    GenerateOutput,
    MatchDecision,
    MatchInput,
    OptionProbability,
    PassageSelection,
    PassageSelectionInput,
    Quantiles,
    QueryPlan,
    QueryPlanInput,
    RoleTask,
    ScenarioScore,
    ScenarioThought,
    ScoreInput,
    ScoreOutput,
)

ALL_SYSTEMS = ["market", "prompt_only", "single_pass", "single_pass_similarity", "pipeline"]
MATCHES = "replays and matches"
NOTE = "Fixed rule in scripts/rehearse_backtest.py, not a model's answer."


def _middle_option(event: EventBrief) -> str:
    return event.options[len(event.options) // 2].outcome_id


def _options(event: EventBrief) -> list[OptionProbability]:
    """Nine tenths on the middle option, the rest shared evenly among the others."""
    if len(event.options) == 1:
        return [OptionProbability(outcome_id=event.options[0].outcome_id, probability=1.0)]
    main, rest = _middle_option(event), 0.1 / (len(event.options) - 1)
    return [
        OptionProbability(
            outcome_id=o.outcome_id, probability=0.9 if o.outcome_id == main else rest
        )
        for o in event.options
    ]


def _quantiles(event: EventBrief) -> Quantiles:
    """Five quantiles taken from the strikes around the middle of the ladder."""
    strikes = sorted(event.thresholds) or [0.0]
    at = [
        strikes[min(len(strikes) - 1, int(len(strikes) * share))]
        for share in (0.3, 0.4, 0.5, 0.6, 0.7)
    ]
    return Quantiles(q05=at[0], q25=at[1], q50=at[2], q75=at[3], q95=at[4])


def _match(payload: Record) -> Record:
    assert isinstance(payload, MatchInput)
    if not payload.candidates:
        return MatchDecision(matched=False, event_ticker="", rationale=NOTE)
    return MatchDecision(
        matched=True, event_ticker=payload.candidates[0].event_ticker, rationale=NOTE
    )


def _plan(payload: Record) -> Record:
    assert isinstance(payload, QueryPlanInput)
    title = payload.event.title
    return QueryPlan(
        query_for=f"{title} evidence for", query_against=f"{title} evidence against", rationale=NOTE
    )


def _select(payload: Record) -> Record:
    assert isinstance(payload, PassageSelectionInput)
    kept = [p.passage_id for p in payload.passages]
    return PassageSelection(keep_passage_ids=kept, dropped=[], rationale=NOTE)


def _generate(payload: Record) -> Record:
    assert isinstance(payload, GenerateInput)
    cite = [p.passage_id for p in payload.passages[:1]]
    outcome_ids: list[str] = []
    low: float | None = None
    high: float | None = None
    if not payload.level.is_resolution:
        claim = f"{payload.level.title} happens as scheduled"
    elif payload.event.outcome_kind == "categorical":
        outcome_ids = [_middle_option(payload.event)]
        claim = f"The event resolves to {outcome_ids[0]}"
    else:
        q = _quantiles(payload.event)
        low, high = q.q25, q.q75 if q.q75 > q.q25 else q.q25 + 0.1
        claim = f"The number comes in between {low} and {high}"
    thought = ScenarioThought(
        claim=claim,
        outcome_ids=outcome_ids,
        value_low=low,
        value_high=high,
        passage_ids=cite,
        rationale=NOTE,
    )
    return GenerateOutput(thoughts=[thought])


def _score(payload: Record) -> Record:
    assert isinstance(payload, ScoreInput)
    ladder = payload.event.outcome_kind == "ladder"
    return ScoreOutput(
        scores=[
            ScenarioScore(
                scenario_id=s.scenario_id,
                verdict="scored",
                probability=0.9,
                option_probabilities=[] if ladder else _options(payload.event),
                quantiles=_quantiles(payload.event) if ladder else None,
                rejection_reason="",
                evidence_request="",
                rationale=NOTE,
            )
            for s in payload.scenarios
        ]
    )


def _baseline(payload: Record) -> Record:
    assert isinstance(payload, BaselineInput)
    if payload.event.outcome_kind == "ladder":
        return BaselineForecast(
            option_probabilities=[], quantiles=_quantiles(payload.event), rationale=NOTE
        )
    return BaselineForecast(
        option_probabilities=_options(payload.event), quantiles=None, rationale=NOTE
    )


def rehearsal_models(config: Config) -> ScriptedModels:
    """The fixed rules above behind the ``Models`` port."""
    return ScriptedModels(
        {
            RoleTask.MATCH: _match,
            RoleTask.PLAN_QUERIES: _plan,
            RoleTask.SELECT_PASSAGES: _select,
            RoleTask.GENERATE: _generate,
            RoleTask.SCORE: _score,
            RoleTask.BASELINE: _baseline,
        },
        config=config,
    )


def rehearse(config: Config, run_dir: Path, *, save_examples: int = 6) -> tuple[bool, list[str]]:
    """Run the rehearsal into ``run_dir``. Returns whether it passed and the summary lines."""
    from pmfa.evaluation.backtest import run_backtest
    from pmfa.evaluation.report import build_report
    from pmfa.evaluation.run_metrics import METRICS_FILE

    shutil.rmtree(run_dir, ignore_errors=True)
    manifest = run_backtest(
        config,
        run_dir=run_dir,
        systems=ALL_SYSTEMS,
        models=rehearsal_models(config),
        save_examples=save_examples,
    )
    report = build_report(run_dir)
    metrics = json.loads((run_dir / METRICS_FILE).read_text(encoding="utf-8"))
    progress = manifest.progress
    lines = [
        "REHEARSAL: every role answer is a fixed rule, so no forecast or score here means anything.",
        f"status: {manifest.status}",
        f"scoring: {len(progress.points_done)} done, {len(progress.points_not_run)} not run "
        "(one per scoring point and system)",
        f"trading triggers: {len(progress.triggers_done)} done, {len(progress.triggers_not_run)} not run",
    ]
    for name, tally in metrics.get("episodes", {}).items():
        exits = ", ".join(f"{k} {v}" for k, v in sorted(tally["exits"].items()))
        lines.append(f"{name} pass: {tally['n_episodes']} pipeline episode(s): {exits or 'none'}")
    for name, halt in (metrics.get("self_halts") or {}).items():
        if halt:
            lines.append(
                f"{name} pass halted itself at {halt['first_started_at']}: {halt['reason']} "
                f"({halt['n_halted_from_then']} of {halt['n_from_then']} episode(s) from then ended halted)"
            )
    late = metrics.get("late_starts")
    if late:
        lines.append(f"late starts: {late['episodes']} of {late['of']} trading episode(s)")
    pricing = metrics.get("fill_pricing") or {}
    if pricing:
        lines.append(
            f"trading orders: {pricing.get('orders', 0)}, by fill candle {pricing.get('by_granularity', {})}, "
            f"filled {pricing.get('filled_by_granularity', {})}"
        )
    for name, verdict in manifest.traces.items():
        lines.append(f"{name}: {verdict}")
    lines.append(f"report: {report}")
    passed = (
        manifest.status == "complete"
        and not progress.points_not_run
        and not progress.triggers_not_run
        and all(verdict == MATCHES for verdict in manifest.traces.values())
    )
    lines.append("rehearsal passed" if passed else "REHEARSAL FAILED: see the lines above")
    return passed, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--run-name", default="rehearsal", help="Run directory name after the date."
    )
    args = parser.parse_args()
    config = load_config()
    run_dir = Path(config.paths.runs_dir) / f"{datetime.now(UTC):%Y-%m-%d}-{args.run_name}"
    passed, lines = rehearse(config, run_dir)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
