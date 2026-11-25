# Evaluation: method and results

The evaluation asks one question: does the agent forecast these markets better than the market price
does, and does the architecture (retrieval, the scenario tree search, separate roles) add anything
over a single prompt? This page describes how the backtest answers it, what has been measured so
far, and how to run the rest. Terms are defined in the [glossary](glossary.md); the pipeline is
drawn in [architecture.md](architecture.md); scoring decisions are recorded in
[decision 0030](decisions/0030-scoring-choices.md) and the data and fill choices in
[decision 0028](decisions/0028-backtest-data-and-fills.md).

The code is in [src/pmfa/evaluation/](../src/pmfa/evaluation/) and the backtest world in
[world/backtest.py](../src/pmfa/world/backtest.py).

## Method

### The frozen data

A backtest never calls a live service. It reads two local files, both rebuilt by one command and
both ignored by git:

- **The exchange snapshot** ([world/snapshot/](../src/pmfa/world/snapshot/)): for each enabled
  followed series (`KXFEDDECISION` and `KXCPIYOY` by default), every event closing between the window
  start and the build date, with its markets, rules, results, and one-minute and hourly candles. The
  builder reads both Kalshi's live and historical endpoints, because settled markets move behind a
  cutoff that advances, and throttles itself to 2 requests per second.
- **The evidence corpus** ([world/evidence/corpus.py](../src/pmfa/world/evidence/corpus.py)):
  Federal Reserve statements, minutes and speeches and BLS CPI and Employment Situation releases
  published in the window, each with its source, URL and publish time, plus the release calendar.
  BLS pages may refuse automated clients; the manual import for a release that is not fetched is
  described in [bls.py](../src/pmfa/world/evidence/bls.py).

```bash
uv run pmfa data build
```

`pmfa data status` prints both manifests. The build on 2026-11-25 produced 7 events, 104 markets,
753,670 one-minute and 87,171 hourly candles (18 minutes), and 55 documents (45 from
federalreserve.gov, 10 from bls.gov).

### The window and the knowledge-cutoff guard

The window starts on 2026-07-01 (`evaluation.window_start`), the day after the June 2026 knowledge
cutoff listed for every default model. A model that may have read about an outcome would leak it, so
`run_backtest` first calls `assert_window_after_cutoffs`
([models/pricing.py](../src/pmfa/models/pricing.py)), which refuses to start the run, naming each
offending slot, if any assigned model's listed cutoff (`models.catalog`) falls on or after the window
start. The manifest records each slot's model, effort and listed cutoff.

### Two schedules

[schedule.py](../src/pmfa/evaluation/schedule.py) builds two deterministic lists from the snapshot
and the corpus:

| Schedule | What it is | Used for |
| --- | --- | --- |
| Scoring points | Every settled event closing in the window, at fixed horizons of 7, 3 and 1 days before close. A point before the window start or before the event opened is skipped, with the reason recorded. | The forecast metrics. Every system forecasts every listed market of the event as of the point. |
| Trading triggers | Every corpus document published in the window, in time order, as a new-document trigger at its publish time. | The trading simulation. The full pipeline runs on one continuous simulated clock, so holdings, the ledger and the bankroll carry forward. The outcome resolver settles what had settled before each trigger, and the rest at the end. |

The 2026-11-25 data gives 21 scoring points (7 events at 3 horizons) and 55 trading triggers.

### Systems compared

| System | What it is | What comparing it isolates |
| --- | --- | --- |
| `market` | The YES mid at the as-of time: the latest candle at or before it, or the last trade when one side of the quote is empty. No model. | The baseline. Brier skill is measured against it. |
| `prompt_only` | One call to the baseline slot (`claude-fable-5-2`, high effort, the strongest model in the pipeline) with the event's rules and markets and no passages. | Against `market`: what the model adds from the question alone. |
| `single_pass` | The same call with the passages retrieved as of the as-of time, ranked by similarity, recency and origin. | Against `prompt_only`: what retrieval adds. |
| `single_pass_similarity` | The same with similarity-only ranking. Not in the default `evaluation.systems`; request it with `--systems`. | Against `single_pass`: what recency- and origin-aware ranking adds. The two retrieve over the same queries, built in code, and differ only in ranking. |
| `pipeline` | A full episode from a schedule trigger: research, the scenario tree with Generator and Critic, aggregation. | Against `single_pass`: what the tree search and separated roles add. It also uses different models per role (see [pmfa.toml](../pmfa.toml)), so the comparison is of the two designs as configured, not of one factor alone. |

No system sees the market price. The pipeline's score uses its forecast before shrinkage toward the
market; shrinkage only enters sizing. A system that makes no forecast (a model error, or a pipeline
episode that ends before forecasting, such as `no_evidence`) abstains, with the reason in the
per-forecast table. Nothing scripted stands in for a model: without an API key, the systems that
need one are recorded as "not run: no API key" and produce no rows.

### Metrics

[metrics.py](../src/pmfa/evaluation/metrics.py), [pnl.py](../src/pmfa/evaluation/pnl.py) and
[summary.py](../src/pmfa/evaluation/summary.py), grouped by
[run_metrics.py](../src/pmfa/evaluation/run_metrics.py):

- **Brier score**: the mean of (probability − outcome)², lower is better, per horizon and pooled.
- **Brier skill against the market**: 1 − system Brier ÷ market Brier on exactly the same rows.
  Above zero beats the market.
- Both are reported two ways: on the forecasts a system made ("answered"), and with every abstention
  scored as the market price, so a system cannot raise its skill by abstaining on hard questions.
- **Reliability**: a table over ten equal-width bins and the expected calibration error.
- **Simulated P&L after fees** of the trading pass, against not trading (which earns zero); the
  equity curve; maximum drawdown; P&L split by routing tier (auto, approve-tier fills tagged as
  would-be-auto, other approve-tier fills).
- **Episode tallies**: exits by reason, gate rejections by reason, routing tiers, and cost, model
  calls and latency per episode.
- **Pruning audit**: how much posterior mass fell on scenarios the search had frozen, and how often
  the most likely path at resolution ran through a frozen one
  ([decision 0025](decisions/0025-pruning-audit.md)).
- **Fill pricing**: how many orders met a one-minute candle, the hourly fallback, or an empty book.
- **Price freshness**: how many market prices came from a mid or a last trade, and how old they were.

### Statistics

Every interval is a 95% percentile bootstrap that resamples whole events, not markets
([bootstrap.py](../src/pmfa/evaluation/bootstrap.py)): 2,000 resamples, seeded with
`evaluation.seed` (20261124), so the same rows give the same interval. Markets in one event resolve
on the same number (five per Fed meeting, twenty or more per CPI release), so resampling markets
would treat correlated rows as independent and overstate the sample. Reports show rows, markets and
events side by side. With fewer than two events there is nothing to resample between, and the bounds
are reported as not defined.

The intervals are wide because the window holds few events: 7 so far. Forecasts of one event at 7, 3
and 1 days are not independent either; the pooled row keeps an event's rows together across
horizons, the per-horizon rows cannot. No difference on this sample separates skill from luck. The
harness is the deliverable; its numbers are a pilot.

### Leakage controls

| Control | Where |
| --- | --- |
| The as-of rule is inside the ports. The snapshot exchange shows quotes, listings and results only up to the simulated clock; the frozen evidence port returns only documents published at or before the as-of time; every retrieval index filters on publish or resolution time inside the Chroma query, before the nearest-neighbour search. | [snapshot/reader.py](../src/pmfa/world/snapshot/reader.py), [evidence/corpus.py](../src/pmfa/world/evidence/corpus.py), [knowledge/index.py](../src/pmfa/knowledge/index.py) |
| A canary document dated one day after the window, containing `evaluation.canary_text` and the words every query uses, sits in the evidence index but not in the corpus. A guard on the knowledge port raises `LeakageDetected` the moment any retrieval returns it, and every finished episode is scanned for it. One hit aborts the run and marks it aborted. | `CanaryGuard`, `assert_episode_clean` in [world/backtest.py](../src/pmfa/world/backtest.py) |
| No web access: the backtest world's exchange is the snapshot, its evidence is the stored corpus, and no port holds an HTTP client. | `build_backtest_world` in [world/backtest.py](../src/pmfa/world/backtest.py) |
| The response cache key includes the as-of time, which every role input carries, so a question asked at a later time is a different entry. | [models/cache.py](../src/pmfa/models/cache.py) |
| The knowledge-cutoff guard described above. | [models/pricing.py](../src/pmfa/models/pricing.py), called first in [evaluation/backtest.py](../src/pmfa/evaluation/backtest.py) |

### Cost controls

- **Dry run.** `--dry-run` writes the manifest with the plan and prints model calls and dollars per
  system, with its token assumptions, and calls no model
  ([estimate.py](../src/pmfa/evaluation/estimate.py)). A call is priced from the mean recorded cost
  of the same task on the same model in the response cache when there is one, and from assumed
  tokens times the catalog price otherwise. The floor assumes every pipeline episode stops after its
  first call; the ceiling assumes a full depth-3 tree.
- **Dollar cap per run.** The gateway's run meter enforces `budget.max_usd_per_run` (default $25)
  before every call ([meter.py](../src/pmfa/models/meter.py)). When it trips, the run stops cleanly,
  is marked partial, and writes everything it ran. `--cap` changes it for one run.
- **Response cache.** Validated answers are stored on disk by content under `data/cache/models`, so
  rerunning the same questions costs nothing.
- **Subsample first.** `--limit N` keeps an evenly spaced subsample of N scoring points and N
  triggers; the manifest lists what was left out.
- **Per-episode caps**: 30 model calls and $2.00 per episode.

### Fill assumptions and their limits

The trading pass fills orders on paper against a book built from one candle
([world/snapshot/book.py](../src/pmfa/world/snapshot/book.py)):

- An order arrives 60 seconds after the as-of time (`execution.backtest_latency_seconds`).
- It meets the one-minute candle containing the arrival time, at the worst touch of that period:
  buying YES pays the period's highest YES ask, buying NO pays 1 minus its lowest YES bid.
- Each level holds 10% of that candle's volume (`execution.backtest_volume_share`), floored to whole
  contracts. Orders are immediate-or-cancel; the unfilled remainder is dropped, and taker fees are
  paid on every fill.
- The liquidity floor is a proxy: dollars traded in the trailing 60 minutes instead of dollars
  resting near the touch.

The limits: Kalshi's history has no order-book depth, so liquidity is inferred from candle volume.
One-minute candles exist only for minutes in which a quote or trade changed, so they are sparse; when
none contains the arrival time, the hourly candle is used and the fill is marked coarse, and when
none exists or the candle had no volume, the book is empty and nothing fills. Each report counts how
every order was priced. Simulated P&L is not evidence of tradable edge. With no human present,
approve-tier proposals are filled and tagged, and reported apart from auto-tier ones.

### What a run writes and what is committed

`pmfa backtest` writes a run directory under `runs/` (ignored by git), named
`<date>-<run name>`: `manifest.json` (configuration, model ids, effort levels and listed cutoffs,
window, commit and whether the working tree was dirty, snapshot and corpus manifests, plan, progress,
usage, and the leakage, cost and fill statements), `forecasts.csv` (one row per system, market and
as-of time, with the outcome, the market price and any abstention note), `trades.csv`,
`episodes.jsonl`, `metrics.json`, and `traces/` (up to three full pipeline episodes saved as
replayable examples, each checked by replaying it). `pmfa report RUN_DIR` adds `report.md` and
`figures/` from the manifest and metrics alone ([report.py](../src/pmfa/evaluation/report.py)).
`--publish NAME` copies `manifest.json`, `forecasts.csv`, `metrics.json`, `report.md`, `figures/` and
`traces/` to `evaluation/runs/NAME/`. `trades.csv` and `episodes.jsonl` stay in the local run
directory, although the manifest's `outputs` list names them.

## Results

### The market baseline (2026-11-25)

The only published run is
[evaluation/runs/2026-11-25-market-baseline/](../evaluation/runs/2026-11-25-market-baseline/report.md).
No Anthropic API key was available, so it scored the market alone; it made no model call and spent
$0.

| Horizon | Forecasts | Markets | Events | Market Brier score | 95% interval |
| --- | --- | --- | --- | --- | --- |
| all | 312 | 104 | 7 | 0.0345 | 0.0108 to 0.0773 |
| 7 days | 104 | 104 | 7 | 0.0377 | 0.0117 to 0.0800 |
| 3 days | 104 | 104 | 7 | 0.0330 | 0.0092 to 0.0759 |
| 1 day | 104 | 104 | 7 | 0.0328 | 0.0095 to 0.0755 |

What these numbers mean:

- They describe the market, not the agent. They are the bar: a system has positive skill only if
  its Brier score on the same rows is below these. The market's skill against itself is 0 by
  definition.
- The scores are low because most rows are easy. 185 of 312 prices sat below 0.10 and 82 above 0.90,
  mostly far strikes of CPI ladders; those rows contribute almost nothing to any system's error, so
  the comparison will be decided by the few rows priced in between.
- The market sharpens slightly toward the close (0.0377 at 7 days, 0.0328 at 1 day), but the
  intervals overlap almost entirely.
- Expected calibration error is 0.0566 over all horizons.
- Prices are often stale: 226 of 312 came from the last trade because one side of the quote was
  empty, the median price was 35.5 hours old, and 54% were older than 24 hours.

### Not yet run

`prompt_only`, `single_pass`, `single_pass_similarity` and `pipeline` have no results, and the
trading simulation has no trades, P&L, routing tiers or pruning audit. Nothing on this page or in the
report describes how they would do.

### Running the systems that need a model

With `ANTHROPIC_API_KEY` set and the data built, in this order.

**1. Dry run.** It calls no model.

```bash
uv run pmfa backtest --dry-run --systems market,prompt_only,single_pass,single_pass_similarity,pipeline
```

On the 2026-11-25 data, without a key, the plan it printed was:

```text
Window: 2026-07-01 to 2026-11-25T09:12:38.877766+00:00; 21 scoring point(s), 55 trading trigger(s)

system                   status                 forecasts   calls (min-max)       USD (min-max)
market                   would run                     21       0-0             0.00-0.00
prompt_only              not run: no API key           21      21-21            5.29-5.29
single_pass              not run: no API key           21      21-21            5.80-5.80
single_pass_similarity   not run: no API key           21      21-21            5.80-5.80
pipeline                 not run: no API key           76      76-1719          0.04-72.17
                          scheduled episodes by tree depth: depth 1: 18, depth 2: 3
                          55 news-triggered episode(s), each bounded with a full depth-3 tree after one Monitor call

Estimated total: $16.93 to $89.05 (run cap $25.0)
```

With the default systems (without `single_pass_similarity`) the total is $11.13 to $83.25. The
estimate uses assumed token counts until the cache holds recorded costs; the high-effort calls are
assumed to write 6,000 output tokens each, thinking included. The upper end exceeds the default $25
cap, so a full run at the default cap would stop partway and be marked partial.

**2. A subsample.** Three scoring points and three triggers; the dry run estimated $1.59 to $6.42 for
the default systems. Its report shows the real cost per call and per episode, and its answers stay in
the cache.

```bash
uv run pmfa backtest --limit 3 --run-name pilot
```

```bash
uv run pmfa report runs/YYYY-MM-DD-pilot
```

**3. The full run**, with a cap chosen from the pilot's measured cost and a fresh dry run (which now
prices calls from the cache). Cached answers from the pilot cost nothing again.

```bash
uv run pmfa backtest --dry-run --systems market,prompt_only,single_pass,single_pass_similarity,pipeline
```

```bash
uv run pmfa backtest --systems market,prompt_only,single_pass,single_pass_similarity,pipeline --cap 90 --run-name full
```

```bash
uv run pmfa report runs/YYYY-MM-DD-full --publish YYYY-MM-DD-full
```

`YYYY-MM-DD` is the date the run started; `pmfa backtest` prints the run directory when it finishes.
Publishing commits the manifest, the per-forecast table, the metrics, the report and figures, and the
saved traces. The notebook [notebooks/evaluation.ipynb](../notebooks/evaluation.ipynb) reads the
newest published run.
