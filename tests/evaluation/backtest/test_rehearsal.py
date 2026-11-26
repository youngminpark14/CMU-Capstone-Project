"""The keyless rehearsal script drives the whole backtest with fixed rules and says if it passed."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from pmfa.config import Config
from pmfa.evaluation.manifest import read_manifest
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
    # Every saved trace was replayed offline and matched.
    assert manifest.traces and set(manifest.traces.values()) == {script.MATCHES}
    # The run records that no live model answered, and the report was written.
    assert manifest.models_source == "supplied"
    assert (run_dir / "report.md").is_file()


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
