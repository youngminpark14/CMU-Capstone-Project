# Notebooks

[evaluation.ipynb](evaluation.ipynb) reads the newest published run under
[evaluation/runs/](../evaluation/runs/) and rebuilds its report's main tables and three figures
from the committed `forecasts.csv` and `metrics.json`. It recomputes every Brier score, skill and
reliability table with the project's own functions in `pmfa.evaluation` and stops if a number
differs from `metrics.json`. It needs no API key and no network, and the notebook is committed
without outputs.

The default environment has no Jupyter kernel. The optional `notebook` dependency group adds one:

```bash
uv sync --group notebook
```

Then open `notebooks/evaluation.ipynb` in Jupyter or an editor that runs notebooks, with the
project's `.venv` as the kernel. The test suite runs the notebook's code cells in plain Python, with
no extra group, and checks its figures and its numbers against `metrics.json`:

```bash
uv run pytest tests/docs/test_notebook.py
```

The method behind the numbers is in [docs/evaluation.md](../docs/evaluation.md).
