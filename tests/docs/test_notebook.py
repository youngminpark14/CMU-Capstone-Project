"""The evaluation notebook runs in plain Python and reproduces the published numbers.

The default environment has no Jupyter, so the code cells are executed in
order in one namespace, from the repository root, with matplotlib's Agg backend.
"""

from __future__ import annotations

import json

import pytest

from .support import ROOT

NOTEBOOK = ROOT / "notebooks" / "evaluation.ipynb"


@pytest.fixture(scope="module")
def namespace() -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4
    ns: dict = {"__name__": "__notebook__"}
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(ROOT)
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                exec(compile("".join(cell["source"]), str(NOTEBOOK), "exec"), ns)
    yield ns
    plt.close("all")


def test_no_outputs_are_committed() -> None:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None


def test_it_reads_the_newest_published_run(namespace: dict) -> None:
    newest = sorted(p for p in (ROOT / "evaluation" / "runs").iterdir() if p.is_dir())[-1]
    assert namespace["RUN_DIR"] == newest


def test_it_draws_the_three_figures(namespace: dict) -> None:
    from matplotlib.figure import Figure

    for name in ("fig_brier", "fig_reliability", "fig_equity"):
        assert isinstance(namespace[name], Figure), name
        assert namespace[name].axes, name


def test_its_brier_numbers_equal_metrics_json(namespace: dict) -> None:
    metrics = json.loads((namespace["RUN_DIR"] / "metrics.json").read_text(encoding="utf-8"))
    recomputed = namespace["recomputed"]
    assert recomputed, "the notebook scored no system"
    for system, by_horizon in recomputed.items():
        for horizon, block in by_horizon.items():
            published = metrics["systems"][system]["by_horizon"][horizon]
            for variant in ("answered", "abstain_as_market"):
                assert block[variant]["brier"] == published[variant]["brier"]
                assert block[variant]["skill"] == published[variant]["skill"]
