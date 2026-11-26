"""Rehearse the whole backtest on the frozen data, with no key and no spending.

Every role answer here comes from a fixed rule in this file, not from a model:
the Monitor takes the first candidate event, the Researcher keeps every passage,
the Generator proposes one scenario, and the Critic and the baselines always
give the same distribution (most of the mass on the middle option, or quantiles
around the middle strike). **So no forecast in a rehearsal means anything, and
nothing it scores is a result.** Its manifest and report say the models were
supplied by the caller, and ``pmfa report --publish`` refuses such a run.

What a rehearsal is for: it drives the pipeline around the models over the real
snapshot and corpus before any money is spent. It builds the retrieval indexes,
runs every scoring point for every system and every news trigger of the trading
pass, sizes and gates orders against real books, fills them from real candles,
applies the stops, saves traces and replays each one, and writes the report. A
rehearsal that passes shows a paid run will not fail on the data or on those
steps.

What it does not exercise is the model side. The fixed rules stand in for the
gateway, so its structured-output schemas, the response cache, cassette
recording, the role runners and the dollar caps are never reached (the
per-episode call cap still applies). The critic here always scores, so critic
rejections and evidence follow-ups never run. And it cannot show how a model's
answers behave.

Run it after ``pmfa data build``, from the repository root::

    uv run python scripts/rehearse_backtest.py

It writes ``runs/<date>-rehearsal`` (ignored by git) and prints a summary. It
exits 0 only when the run completed, every pipeline episode's answers were
accepted, forecasts were made, at least one order was sent, and every saved
trace replays and matches. It refuses to write into a directory that holds a
run it did not make.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pmfa.config import Config, load_config
from pmfa.evaluation.manifest import RunManifest
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
REHEARSAL_NOTE = (
    "REHEARSAL: every role answer in this run is a fixed rule in scripts/rehearse_backtest.py. "
    "Nothing in it is a result."
)
BROKEN_EXITS = frozenset({"rules_mismatch", "low_coverage", "error"})
"""Exits a fixed rule must never cause: its answer was refused, or a step failed."""


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


def _not_a_rehearsal(run_dir: Path) -> bool:
    """Whether ``run_dir`` holds something a rehearsal (or another supplied-models run) did not write."""
    from pmfa.evaluation.manifest import MANIFEST_FILE, read_manifest

    if not run_dir.exists():
        return False
    if not (run_dir / MANIFEST_FILE).exists():
        return True
    return read_manifest(run_dir).models_source != "supplied"


def _problems(run_dir: Path, manifest: RunManifest, metrics: dict[str, Any]) -> list[str]:
    """What stops this rehearsal from passing: each entry is one thing that was not decided as it should be."""
    from pmfa.evaluation.backtest import FORECASTS_FILE

    problems: list[str] = []
    progress = manifest.progress
    if manifest.status != "complete":
        problems.append(f"the run's status is {manifest.status}")
    if progress.points_not_run or progress.triggers_not_run:
        problems.append("some scoring points or trading triggers were not run")
    episodes = metrics.get("episodes") or {}
    if set(episodes) != {"scoring", "trading"}:
        problems.append("the pipeline did not run in both passes")
    executed = 0
    for name, tally in episodes.items():
        exits = tally["exits"]
        refused = {k: v for k, v in exits.items() if k in BROKEN_EXITS}
        if refused:
            problems.append(
                f"{name} pass: a fixed rule's answer was refused or a step failed: {refused}"
            )
        if not set(exits) - {"no_match"}:
            problems.append(f"{name} pass: no pipeline episode got past matching")
        executed += exits.get("executed", 0)
    if not executed:
        problems.append("no pipeline episode sent an order")
    with (run_dir / FORECASTS_FILE).open(encoding="utf-8", newline="") as fh:
        made = [r for r in csv.DictReader(fh) if r["system"] == "pipeline" and r["probability"]]
    if not made:
        problems.append("the pipeline made no forecast")
    if not manifest.traces:
        problems.append("no trace was saved")
    elif any(verdict != MATCHES for verdict in manifest.traces.values()):
        problems.append("a saved trace does not replay to what was recorded")
    return problems


def rehearse(config: Config, run_dir: Path, *, save_examples: int = 6) -> tuple[bool, list[str]]:
    """Run the rehearsal into ``run_dir``. Returns whether it passed and the summary lines.

    Raises ``ValueError`` when ``run_dir`` holds a run a rehearsal did not make:
    the directory is replaced, and a paid run must never be the one replaced.
    """
    from pmfa.evaluation.backtest import run_backtest
    from pmfa.evaluation.manifest import write_manifest
    from pmfa.evaluation.report import build_report
    from pmfa.evaluation.run_metrics import METRICS_FILE

    if _not_a_rehearsal(run_dir):
        raise ValueError(
            f"{run_dir} holds a run this script did not make; choose another --run-name"
        )
    shutil.rmtree(run_dir, ignore_errors=True)
    manifest = run_backtest(
        config,
        run_dir=run_dir,
        systems=ALL_SYSTEMS,
        models=rehearsal_models(config),
        save_examples=save_examples,
    )
    manifest = manifest.model_copy(update={"notes": [REHEARSAL_NOTE, *manifest.notes]})
    write_manifest(manifest, run_dir)
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
    for name, tally in (metrics.get("episodes") or {}).items():
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
    problems = _problems(run_dir, manifest, metrics)
    lines.extend(f"PROBLEM: {problem}" for problem in problems)
    lines.append("rehearsal passed" if not problems else "REHEARSAL FAILED: see the problems above")
    return not problems, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--run-name", default="rehearsal", help="Run directory name after the date."
    )
    args = parser.parse_args()
    config = load_config()
    run_dir = Path(config.paths.runs_dir) / f"{datetime.now(UTC):%Y-%m-%d}-{args.run_name}"
    try:
        passed, lines = rehearse(config, run_dir)
    except ValueError as exc:
        sys.stderr.write(f"rehearsal refused: {exc}\n")
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
