"""The keyless rehearsal script drives the whole backtest with fixed rules and says if it passed."""

from __future__ import annotations

import csv
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

from pmfa.config import Config
from pmfa.evaluation.manifest import read_manifest
from pmfa.evaluation.report import publish_run
from pmfa.evaluation.run_metrics import METRICS_FILE
from pmfa.records.roles import EventBrief, OptionBrief
from tests.evaluation.backtest.support import WINDOW_END

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "rehearse_backtest.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rehearse_backtest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_rehearsal_runs_every_system_saves_traces_that_replay_and_says_it_is_not_a_result(
    data_config: Config, tmp_path: Path
) -> None:
    script = load_script()
    run_dir = tmp_path / "run"
    passed, lines = script.rehearse(data_config, run_dir)
    assert passed and lines[-1] == "rehearsal passed"
    manifest = read_manifest(run_dir)
    assert manifest.status == "complete"
    assert manifest.systems == dict.fromkeys(script.ALL_SYSTEMS, "run")
    assert not manifest.progress.points_not_run and not manifest.progress.triggers_not_run
    # What was decided: every fixed answer was accepted, forecasts were made, orders were sent.
    episodes = json.loads((run_dir / METRICS_FILE).read_text(encoding="utf-8"))["episodes"]
    assert set(episodes) == {"scoring", "trading"}
    for tally in episodes.values():
        assert not script.BROKEN_EXITS & set(tally["exits"])
    assert sum(tally["exits"].get("executed", 0) for tally in episodes.values()) > 0
    with (run_dir / "forecasts.csv").open(encoding="utf-8", newline="") as fh:
        assert any(r["system"] == "pipeline" and r["probability"] for r in csv.DictReader(fh))
    # The scoring pass settled before its points: some critics were given calibration examples.
    [given] = [line for line in lines if line.startswith("scoring pass: the critic was given")]
    with_examples, reached = map(int, re.findall(r"in (\d+) of (\d+) ", given)[0])
    assert 0 < with_examples < reached
    # Every saved trace was replayed offline and matched.
    assert manifest.traces and set(manifest.traces.values()) == {script.MATCHES}
    # The run records that no live model answered, says it is a rehearsal, and cannot be published.
    assert manifest.models_source == "supplied"
    assert manifest.notes[0] == script.REHEARSAL_NOTE
    assert (run_dir / "report.md").is_file()
    with pytest.raises(ValueError, match="not a result"):
        publish_run(run_dir, "rehearsal", root=tmp_path / "published")
    # A second rehearsal may replace the first.
    again, _ = script.rehearse(data_config, run_dir)
    assert again


def test_a_rehearsal_whose_fixed_answers_are_refused_does_not_pass(
    data_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run can complete, and its traces replay, while no forecast was ever made."""
    script = load_script()
    generate = script._generate

    def names_an_option_that_does_not_exist(payload):
        output = generate(payload)
        thoughts = [
            t.model_copy(
                update={"outcome_ids": ["no-such-option"], "value_low": None, "value_high": None}
            )
            if payload.level.is_resolution
            else t
            for t in output.thoughts
        ]
        return output.model_copy(update={"thoughts": thoughts})

    monkeypatch.setattr(script, "_generate", names_an_option_that_does_not_exist)
    passed, lines = script.rehearse(data_config, tmp_path / "run")
    assert not passed and lines[-1].startswith("REHEARSAL FAILED")
    assert read_manifest(tmp_path / "run").status == "complete"  # the weak signal still looks fine
    problems = [line for line in lines if line.startswith("PROBLEM: ")]
    assert problems and all("rules_mismatch" in p for p in problems)


def test_a_rehearsal_refuses_to_replace_a_run_it_did_not_make(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.backtest import run_backtest

    script = load_script()
    real = tmp_path / "paid-run"
    run_backtest(data_config, run_dir=real, systems=["market"])  # not answered by supplied models
    kept = sorted(p.name for p in real.iterdir())
    with pytest.raises(ValueError, match="did not make"):
        script.rehearse(data_config, real)
    assert sorted(p.name for p in real.iterdir()) == kept
    stray = tmp_path / "notes"
    stray.mkdir()
    (stray / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ValueError, match="did not make"):
        script.rehearse(data_config, stray)
    assert (stray / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_the_fixed_rules_give_a_distribution_for_any_event_shape() -> None:
    script = load_script()

    def event(**fields: object) -> EventBrief:
        base = {
            "event_ticker": "EV",
            "title": "An event",
            "category": "c",
            "closes_at": WINDOW_END,
            "rules": "",
        }
        return EventBrief.model_validate({**base, **fields})

    one = event(outcome_kind="categorical", options=[OptionBrief(outcome_id="only", label="Only")])
    assert [(o.outcome_id, o.probability) for o in script._options(one)] == [("only", 1.0)]
    five = event(
        outcome_kind="categorical",
        options=[OptionBrief(outcome_id=k, label=k) for k in "abcde"],
    )
    probabilities = {o.outcome_id: o.probability for o in script._options(five)}
    assert probabilities["c"] == 0.9 and abs(sum(probabilities.values()) - 1.0) < 1e-9
    # A ladder with a single strike still gives non-decreasing quantiles.
    single = script._quantiles(event(outcome_kind="ladder", thresholds=[3.0]))
    assert (single.q05, single.q95) == (3.0, 3.0)
    ladder = script._quantiles(event(outcome_kind="ladder", thresholds=[3.0, 3.1, 3.2, 3.3, 3.4]))
    assert ladder.q05 <= ladder.q25 <= ladder.q50 <= ladder.q75 <= ladder.q95
    assert (ladder.q05, ladder.q50, ladder.q95) == (3.1, 3.2, 3.3)
