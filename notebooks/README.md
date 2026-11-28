# Notebooks

[evaluation.ipynb](evaluation.ipynb) reads the newest published run under
[evaluation/runs/](../evaluation/runs/) and rebuilds its report's main tables and three figures.

- A run published with its per-forecast table (`forecasts.csv`) is scored again: the notebook
  recomputes every Brier score, skill and reliability table from that table with the project's own
  functions in `pmfa.evaluation`, and stops if a number differs from `metrics.json`.
- A run published as scores only has no per-forecast table in the repository. The notebook draws
  its tables and figures from `metrics.json` and says that nothing was recomputed. A bin of the
  market's reliability table whose mean was withheld (fewer than five forecasts) reads `withheld`,
  an empty bin `empty`, and a calibration error withheld with such a mean reads `withheld`. The
  reliability table that scores abstentions as the market price, and its calibration error, are
  left out of such a publication; the notebook says so in a sentence.

It needs no API key and no network, and the notebook is committed without outputs.

The default environment has no Jupyter kernel. The optional `notebook` dependency group adds one:

```bash
uv sync --group notebook
```

Then open `notebooks/evaluation.ipynb` in Jupyter or an editor that runs notebooks, with the
project's `.venv` as the kernel. The test suite runs the notebook's code cells in plain Python, with
no extra group, and checks its figures and its numbers against `metrics.json`, for a run with its
per-forecast table and for one without:

```bash
uv run pytest tests/docs/test_notebook.py
```

The method behind the numbers is in [docs/evaluation.md](../docs/evaluation.md).
