"""The evaluation notebook runs in plain Python and reproduces the published numbers.

The default environment has no Jupyter, so the code cells are executed in
order in one namespace, with matplotlib's Agg backend. The notebook reads the
newest published run under the directory it is run from. A run published with
its per-forecast table is scored again from that table; a run published as
scores only is drawn from its ``metrics.json``. Both are run here: the
repository's own newest run, and the market baseline copied to a temporary
directory with and without its table.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from .support import ROOT

NOTEBOOK = ROOT / "notebooks" / "evaluation.ipynb"
RUNS = ROOT / "evaluation" / "runs"
WITH_ROWS = RUNS / "2026-11-26-market-baseline"
"""A published run whose per-forecast table is committed."""
FIGURES = ("fig_brier", "fig_reliability", "fig_equity")


@pytest.fixture(scope="module", autouse=True)
def agg_backend():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    yield
    plt.close("all")


def run_notebook(root: Path) -> dict:
    """Run the notebook's code cells in order, in one namespace, from ``root``."""
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4
    ns: dict = {"__name__": "__notebook__"}
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(root)
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                exec(compile("".join(cell["source"]), str(NOTEBOOK), "exec"), ns)
    return ns


def copied_run(tmp_path: Path, *, rows: bool) -> Path:
    """A directory whose only published run is the market baseline, with or without its table.

    Without its table it is published as scores only, so its metrics are filtered the same way.
    """
    from pmfa.evaluation.report import _scores_only_metrics

    target = tmp_path / "evaluation" / "runs" / WITH_ROWS.name
    target.mkdir(parents=True)
    for name in ("manifest.json", "metrics.json", *(["forecasts.csv"] if rows else [])):
        shutil.copy(WITH_ROWS / name, target / name)
    if not rows:
        metrics = json.loads((target / "metrics.json").read_text(encoding="utf-8"))
        (target / "metrics.json").write_text(json.dumps(_scores_only_metrics(metrics)))
    return tmp_path


def assert_scores_equal_metrics(scores: dict, metrics: dict) -> None:
    assert scores, "the notebook shows no system's scores"
    for system, by_horizon in scores.items():
        for horizon, block in by_horizon.items():
            published = metrics["systems"][system]["by_horizon"][horizon]
            for variant in ("answered", "abstain_as_market"):
                assert block[variant]["brier"] == published[variant]["brier"]
                assert block[variant]["skill"] == published[variant]["skill"]


def assert_figures_drawn(namespace: dict) -> None:
    from matplotlib.figure import Figure

    for name in FIGURES:
        assert isinstance(namespace[name], Figure), name
        assert namespace[name].axes, name


@pytest.fixture(scope="module")
def namespace() -> dict:
    """The notebook run from the repository root: the repository's newest published run."""
    return run_notebook(ROOT)


def test_no_outputs_are_committed() -> None:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None


def test_it_reads_the_newest_published_run(namespace: dict) -> None:
    newest = sorted(p for p in RUNS.iterdir() if p.is_dir())[-1]
    assert namespace["RUN_DIR"] == newest
    assert namespace["HAS_ROWS"] == (newest / "forecasts.csv").is_file()


def test_it_draws_the_three_figures(namespace: dict) -> None:
    assert_figures_drawn(namespace)


def test_its_brier_numbers_equal_metrics_json(namespace: dict) -> None:
    metrics = json.loads((namespace["RUN_DIR"] / "metrics.json").read_text(encoding="utf-8"))
    assert_scores_equal_metrics(namespace["scores"], metrics)


def test_a_run_published_with_its_rows_is_scored_again_from_them(tmp_path: Path) -> None:
    namespace = run_notebook(copied_run(tmp_path, rows=True))
    metrics = json.loads((WITH_ROWS / "metrics.json").read_text(encoding="utf-8"))
    assert namespace["HAS_ROWS"] and namespace["rows"]
    # Recomputed from the rows by the notebook, not read back from the file it loaded.
    assert namespace["recomputed"] is namespace["scores"]
    loaded = namespace["metrics"]["systems"]["market"]["by_horizon"]
    assert namespace["scores"]["market"] is not loaded
    assert_scores_equal_metrics(namespace["recomputed"], metrics)
    assert_figures_drawn(namespace)


def test_a_run_published_as_scores_only_is_drawn_from_metrics_json_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    namespace = run_notebook(copied_run(tmp_path, rows=False))
    printed = " ".join(capsys.readouterr().out.split())
    metrics = json.loads((WITH_ROWS / "metrics.json").read_text(encoding="utf-8"))
    assert not namespace["HAS_ROWS"] and namespace["rows"] == []
    assert namespace["recomputed"] == {}  # nothing is claimed to be recomputed
    assert "published as scores only" in printed and "nothing is recomputed" in printed
    assert namespace["scores"]["market"] == metrics["systems"]["market"]["by_horizon"]
    assert set(namespace["tables"]) == {"market"}
    # The published metrics withhold the mean forecast of a bin with few forecasts. The
    # notebook shows what was published and still draws its figures.
    from pmfa.evaluation.report import _scores_only_metrics

    published = _scores_only_metrics(metrics)["reliability"]["market"]["answered"]
    assert namespace["tables"]["market"].model_dump(mode="json") == published
    assert any(b["count"] and b["mean_forecast"] is None for b in published["bins"])
    assert published != metrics["reliability"]["market"]["answered"]
    # A withheld mean reads "withheld"; an empty bin reads "empty"; the two never look alike.
    for b in published["bins"]:
        row = f"| [{b['lower']:.1f}, {b['upper']:.1f}) | {b['count']} |"
        if not b["count"]:
            assert f"{row} empty | empty |" in printed
        elif b["mean_forecast"] is None:
            assert f"{row} withheld | {b['observed_frequency']:.3f} |" in printed
    # The table that scores abstentions as the market, and its error, are left out.
    other = metrics["reliability"]["market"]["abstain_as_market"]
    assert (
        f"With abstentions scored as the market price ({other['n_rows']} forecasts): the table "
        "and its ECE are left out of a scores-only publication."
    ) in printed
    assert_figures_drawn(namespace)
