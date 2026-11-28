"""What a run says each system did, and what the report shows for a pass that did not run."""

from __future__ import annotations

from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.evaluation.backtest import read_forecasts, run_backtest
from tests.evaluation.backtest.support import ALL_SYSTEMS, CappedModels, scripted

MODEL_SYSTEMS = ("prompt_only", "single_pass", "single_pass_similarity", "pipeline")


def _scoring_calls(config: Config, tmp_path: Path) -> int:
    probe = CappedModels(scripted(config), 10**6)
    run_backtest(config, run_dir=tmp_path / "probe", systems=ALL_SYSTEMS, models=probe, trade=False)
    return probe.made


def test_a_cap_on_the_first_call_marks_unstarted_systems_not_run(
    data_config: Config, tmp_path: Path
) -> None:
    manifest = run_backtest(
        data_config,
        run_dir=tmp_path / "run",
        systems=ALL_SYSTEMS,
        models=CappedModels(scripted(data_config), 0),
    )
    assert manifest.status == "partial"
    # The market scored the first point before the first model call tripped the cap.
    assert manifest.systems["market"].startswith("partial: 1 of 6 points")
    for system in MODEL_SYSTEMS:
        assert manifest.systems[system].startswith("not run:"), system
        assert "dollar cap" in manifest.systems[system], system


def test_a_cap_in_the_trading_pass_leaves_the_scoring_systems_run(
    data_config: Config, tmp_path: Path
) -> None:
    allowed = _scoring_calls(data_config, tmp_path)
    manifest = run_backtest(
        data_config,
        run_dir=tmp_path / "run",
        systems=ALL_SYSTEMS,
        models=CappedModels(scripted(data_config), allowed),
    )
    assert manifest.status == "partial"
    assert manifest.progress.points_not_run == []
    for system in ("market", "prompt_only", "single_pass", "single_pass_similarity"):
        assert manifest.systems[system] == "run", system
    # The first trigger's episode ran until the cap ended it (exit error), so it counts as run.
    assert manifest.systems["pipeline"].startswith("partial: 6 of 6 points, 1 of 3 triggers")


def test_a_cap_in_the_last_trading_trigger_still_leaves_the_pipeline_partial(
    data_config: Config, tmp_path: Path
) -> None:
    """The capped trigger counts as done (its episode ran and ended error), so a count of
    every trigger does not mean the pass was whole."""
    probe = CappedModels(scripted(data_config), 10**6)
    run_backtest(
        data_config, run_dir=tmp_path / "probe", systems=["pipeline"], models=probe, score=False
    )
    manifest = run_backtest(
        data_config,
        run_dir=tmp_path / "run",
        systems=["pipeline"],
        models=CappedModels(scripted(data_config), probe.made - 1),
        score=False,
    )
    assert manifest.status == "partial"
    assert manifest.progress.triggers_not_run == []
    assert manifest.systems["pipeline"].startswith("partial: 0 of 0 points, 3 of 3 triggers")
    assert "dollar cap" in manifest.systems["pipeline"]


def test_unrequested_systems_are_not_requested_even_without_a_key(
    data_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pmfa.evaluation.report import build_report

    monkeypatch.delenv(data_config.models.api_key_env, raising=False)
    run_dir = tmp_path / "run"
    manifest = run_backtest(data_config, run_dir=run_dir, systems=["market"])
    assert manifest.systems["market"] == "run"
    for system in MODEL_SYSTEMS:
        assert manifest.systems[system] == "not requested", system
    assert "no API key" not in build_report(run_dir).read_text()


def test_no_pnl_numbers_when_the_trading_simulation_did_not_run(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = tmp_path / "run"
    manifest = run_backtest(
        data_config, run_dir=run_dir, systems=ALL_SYSTEMS, models=scripted(data_config), trade=False
    )
    assert manifest.systems["pipeline"] == "run"  # it ran in the scoring pass
    text = build_report(run_dir).read_text()
    trading = text.split("## Simulated trading after fees", 1)[1].split("\n## ", 1)[0]
    assert "Realized P&L" not in trading and "drawdown" not in trading
    assert "did not run" in trading and "--no-trade" in trading


def test_no_pnl_numbers_when_the_run_stopped_before_the_trading_pass(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = tmp_path / "run"
    run_backtest(
        data_config,
        run_dir=run_dir,
        systems=ALL_SYSTEMS,
        models=CappedModels(scripted(data_config), 9),
    )
    text = build_report(run_dir).read_text()
    trading = text.split("## Simulated trading after fees", 1)[1].split("\n## ", 1)[0]
    assert "Realized P&L" not in trading and "drawdown" not in trading
    assert "did not run" in trading


def test_a_partial_run_marks_rows_scored_on_fewer_points(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    run_dir = tmp_path / "run"
    manifest = run_backtest(
        data_config,
        run_dir=run_dir,
        systems=ALL_SYSTEMS,
        models=CappedModels(scripted(data_config), 9),
    )
    rows = read_forecasts(run_dir / "forecasts.csv")
    points = {
        s: {(r.event_ticker, r.horizon_days) for r, _ in rows if r.system == s} for s in ALL_SYSTEMS
    }
    assert len(points["market"]) > len(points["pipeline"]) > 0
    assert manifest.systems["pipeline"].startswith(f"partial: {len(points['pipeline'])} of 6")
    text = build_report(run_dir).read_text()
    brier = text.split("## Brier score and Brier skill", 1)[1].split("\n## ", 1)[0]
    assert "different sets of points" in brier
    assert f"Full pipeline ({len(points['pipeline'])} of 6 points)" in brier


def test_the_leakage_section_says_what_the_canary_guarded(
    data_config: Config, tmp_path: Path
) -> None:
    from pmfa.evaluation.report import build_report

    market_dir = tmp_path / "market"
    manifest = run_backtest(data_config, run_dir=market_dir, systems=["market"])
    assert manifest.leakage_checks["index_built"] is False
    assert manifest.leakage_checks["retrievals_checked"] == 0
    leakage = build_report(market_dir).read_text().split("## How look-ahead was prevented", 1)[1]
    leakage = leakage.split("\n## ", 1)[0]
    assert "the canary was not exercised" in leakage
    assert "none returned it" not in leakage

    run_dir = tmp_path / "baselines"
    manifest = run_backtest(
        data_config,
        run_dir=run_dir,
        systems=["market", "single_pass", "single_pass_similarity"],
        models=scripted(data_config),
    )
    checks = manifest.leakage_checks
    # Six points, each retrieved once per single-pass variant (the ranking variants share a count).
    assert checks["index_built"] is True and checks["retrievals_checked"] == 12
    leakage = build_report(run_dir).read_text().split("## How look-ahead was prevented", 1)[1]
    assert "guarded 12 retrieval call(s) and 0 finished episode(s)" in leakage
    assert "missing altogether" in leakage
