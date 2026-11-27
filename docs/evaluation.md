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

## Where the project stands

| Area | State |
| --- | --- |
| Pipeline and tests | Built end to end. The offline suite of 1,369 tests runs with no keys and no network in about two minutes on one machine (1,365 passed in 123 seconds on 2026-11-27). Four tests are skipped by default: the three [opt-in smoke tests](#opt-in-smoke-tests), and a test that needs a locally cached embedding model. |
| Episode scenarios | All sixteen scenarios from the specification pass in [tests/episodes/test_scenarios.py](../tests/episodes/test_scenarios.py). |
| Offline replay | Five sample episodes replay with one command and match their committed outputs. |
| Real data | The exchange snapshot (7 settled events, 104 markets) and the evidence corpus (45 Federal Reserve and 10 BLS documents) were built from the live public sources on 2026-11-25. The counts are in the published [manifest](../evaluation/runs/2026-11-26-market-baseline/manifest.json). The whole backtest has been rehearsed on this data with fixed-rule role answers in place of a model ([scripts/rehearse_backtest.py](../scripts/rehearse_backtest.py)), which checks the data and the pipeline around the models but not the model side (the gateway, the cache, the dollar caps): its forecasts mean nothing. |
| Measured | Of forecasting, only the market's own: its Brier score and calibration error (see [Results](#results)). They are the baseline every later run is compared with. The other measured figures here, such as fill coverage, price staleness and build time, describe the data and the harness, not forecasting. |
| Not yet done | **No backtest or other evaluation with a model has been run yet.** The published run is the market's alone, made when no Anthropic API key was available. The agent's forecasts have therefore not been measured. |
| Sample episodes | Hand-made fixtures: the inputs are real, but every model record is fixed text written in advance with an AI coding assistant, not the output of the pipeline's roles. They show the pipeline working, not forecasting skill, and each one says so. |
| Shadow mode | Exercised for plumbing only, against recorded worlds in [tests/live/](../tests/live/). |
| Demo executor | Built from Kalshi's documentation and tested against recorded and constructed responses. A read-only check against a live Kalshi demo account passed on 2026-11-27: signed requests, the balance and market data. No order has been placed on the demo exchange yet, and the exchange-side protections have not been exercised ([guardrails](guardrails.md#what-is-not-guaranteed)). |
| Fees | The fee formula and rounding are tested on hand-computed cases and one worked example from Kalshi's fee-rounding documentation. The fee schedule PDF's own worked examples are not in the tests: the PDF sits behind a browser check. |

Beating the market price is the hypothesis the evaluation tests, not a result of this project.

### Opt-in smoke tests

Three tests call live services and are skipped unless `PMFA_SMOKE=1` is set:

```bash
PMFA_SMOKE=1 uv run pytest -m smoke
```

- one read of a series from Kalshi production (public data, no key);
- a read-only check of the demo exchange: the balance and one market, no order. It needs
  `KALSHI_DEMO_KEY_ID` and `KALSHI_DEMO_PRIVATE_KEY` or `KALSHI_DEMO_PRIVATE_KEY_PATH`;
- one model call through the gateway on the cheapest configured model. It needs
  `ANTHROPIC_API_KEY`.

A test whose keys are missing is skipped. CI never sets `PMFA_SMOKE`.

## Method

### The frozen data

A backtest never calls a live service. It reads two local files, both rebuilt by one command and
both ignored by git:

- **The exchange snapshot** ([world/snapshot/](../src/pmfa/world/snapshot/)): for each enabled
  followed series (`KXFEDDECISION` and `KXCPIYOY` by default), every event closing between the window
  start and the build time, with its markets, rules, results, and one-minute and hourly candles. The
  builder reads both Kalshi's live and historical endpoints, because settled markets move behind a
  cutoff that advances, and throttles itself to 2 requests per second.
- **The evidence corpus** ([world/evidence/corpus.py](../src/pmfa/world/evidence/corpus.py)):
  Federal Reserve statements, minutes and speeches and BLS CPI and Employment Situation releases
  published in the window, each with its source, URL and publish time, plus the release calendar.
  BLS pages may refuse automated clients. When a release is not fetched, save it from a browser
  into `data/manual/` as `bls-cpi-YYYY-MM.html` (the reference month, for example
  `bls-cpi-2026-10.html` for the October CPI) and run `pmfa data build` again. The steps are in the
  docstring of [bls.py](../src/pmfa/world/evidence/bls.py).

```bash
uv run pmfa data build
```

This step reads public Kalshi market data and Federal Reserve and BLS pages and needs no key.
`pmfa data status` prints both manifests. The build on 2026-11-25 produced 7 events, 104 markets,
753,670 one-minute and 87,171 hourly candles, and 55 documents (45 from federalreserve.gov, 10 from
bls.gov). The snapshot build is resumable: a range already stored is skipped, so a second run
continues where the first stopped.

A snapshot has two hashes. Its content digest (`content_sha256` in
[store.py](../src/pmfa/world/snapshot/store.py)) is a SHA-256 over its data rows alone: series,
events, markets, candles and coverage, keyed by ticker in a fixed order, so it does not depend on
insertion order, internal row ids or the file's layout. The file hash is the SHA-256 of the file's
bytes. It differs on every rebuild, because the file also holds the build manifest and its page
layout depends on the build. `pmfa data build` prints the content digest (`snapshot: content SHA-256
...`) and records it in the snapshot manifest as `content_sha256`. A build clears the recorded
digest before it changes any row, so a build that stops part way leaves none. So does a build made
before the digest existed, such as the one the published run read: its snapshot manifest's
`content_sha256` is null. A run then computes the digest from the rows. To check that a rebuild
holds the data a published run read, compare the digest the build prints with the run manifest's
`snapshot_content_sha256`, which the report prints first on its Data line. The snapshot holds every
event closing between the window start and the build time, so a rebuild made after another followed
event has closed holds that event too and gives a different digest.

`pmfa data build --end YYYY-MM-DD` ends the window, for the snapshot and the corpus, at the end of
that day instead of at the build time ([cli/data.py](../src/pmfa/cli/data.py)). It takes a day, so
it cannot end the window at a run's exact build time. For the published run it would end at
2026-11-25 23:59:59 UTC, after the snapshot's window end (20:10:48) and well after the corpus's
(09:12:38; `window_end` in each manifest), so a corpus rebuilt that way may hold documents published
in those hours. The snapshot's digest would differ only if an event closed in them. A rebuild with
`--end` has not been tried, so whether it gives the published digest is not known. A published run
names its corpus by one digest of the per-document content hashes (`corpus_hashes_digest`, computed
by `hashes_digest` in [manifest.py](../src/pmfa/evaluation/manifest.py)), which the report prints as
the corpus texts digest on the same Data line. `pmfa data status` prints the per-document hashes but
not that digest, so to compare a rebuilt corpus, compute `hashes_digest` over them.

A from-scratch rebuild on 2026-11-25 took 1,532 seconds (about 25 minutes), 1,408 of them for the
snapshot at 2 requests per second. It reproduced every table of the snapshot (2 series, 7 events,
104 markets, 840,841 candles, 208 coverage rows). Both snapshot files give the content digest
`15d471e2b2b6700e7bb137355c241c65df1d67ff0cca68fd2355464682992638`, while their file hashes differ
(`a9c3334d...` for the data the published run read, `a5155b3f...` for the rebuild).

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
| Scoring points | Every settled event closing in the window, at fixed horizons of 7, 3 and 1 days before close. A point before the window start or before the event opened is skipped, with the reason recorded. | The forecast metrics. Every system forecasts every listed market of the event as of the point. When the pipeline runs, the outcome resolver writes back, before each point's forecasts, every result the snapshot exchange shows as settled at the point's as-of time: the pipeline's paper account settles those markets, its stops read them, and its Critic is given up to five of the pipeline's earlier forecasts that resolved before the as-of time (counted in whole seconds), same category first. The market and the one-call baselines read no outcome. The pipeline's episodes size, gate and fill on one paper account, and its orders are not reported as trades. |
| Trading triggers | Every corpus document published in the window, in time order, as a new-document trigger at its publish time. | The trading simulation. The full pipeline runs on one continuous simulated clock, so holdings, the ledger and the bankroll carry forward. The outcome resolver settles what had settled before each episode starts (at its trigger's time, or later after a late start; see "Leakage controls"), and the rest at the end, once the last order's outcome has passed. |

The 2026-11-25 data gives 21 scoring points (7 events at 3 horizons) and 55 trading triggers.

### Systems compared

| System | What it is | What comparing it isolates |
| --- | --- | --- |
| `market` | The market's price at the as-of time, from the latest candle at or before it: the YES mid when both sides are quoted, else the last trade clamped into the standing quote (a trade from weeks ago above a no-bid ask scores at the ask). No model. | The baseline. Brier skill is measured against it. |
| `prompt_only` | One call to the baseline slot (`claude-fable-5-2`, high effort, the strongest model in the pipeline) with the event's rules and markets and no passages. | Against `market`: what the model adds from the question alone. |
| `single_pass` | The same call with the passages retrieved as of the as-of time, ranked by similarity, recency and origin. | Against `prompt_only`: what retrieval adds. |
| `single_pass_similarity` | The same with similarity-only ranking. Not in the default `evaluation.systems`; request it with `--systems`. | Against `single_pass`: what recency- and origin-aware ranking adds. The two retrieve over the same queries, built in code, and differ only in ranking. |
| `pipeline` | A full episode from a schedule trigger: research, the scenario tree with Generator and Critic, aggregation. | Against `single_pass`: what the tree search and separated roles add. It also uses different models per role (see [pmfa.toml](../pmfa.toml)), so the comparison is of the two designs as configured, not of one factor alone. |

No system sees the market price. The pipeline's score uses its forecast before shrinkage toward the
market; shrinkage only enters sizing. A system that makes no forecast (a model error, or a pipeline
episode that ends before forecasting, such as `no_evidence`) abstains, with the reason in the
per-forecast table. In `pmfa backtest` nothing scripted stands in for a model: without an API key,
the systems that need one are recorded, when requested, as "not run: no API key" and produce no
rows. The rehearsal script below supplies fixed rules instead, and `pmfa report --publish` refuses
such a run.

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
- **Where the error comes from**: each system's summed squared error split by forecast
  probability (below 0.10, 0.10 to 0.90, above 0.90), with the share of its largest event and of
  its three largest markets.
- **Episode tallies**: exits by reason, gate rejections by reason, routing tiers, and cost, model
  calls and latency per episode; for each pass, the first episode that ended `halted` and how many
  ended halted from then on.
- **Pruning audit**: how much posterior mass fell on scenarios the search had frozen, and how often
  the most likely path at resolution ran through a frozen one
  ([decision 0025](decisions/0025-pruning-audit.md)).
- **Fill pricing**: how many orders met a one-minute candle, the hourly fallback, or an empty book.
- **Where the market price came from**: how many prices were a mid and how many a last trade (and
  how many of those were clamped into the quote), with the quote's age (time since it last changed,
  which is not staleness: a standing quote is still the quote) and the last trade's age reported
  apart.
- **Left out of scoring**: calendar releases with no event in the snapshot, events the snapshot
  builder dropped (recorded in the snapshot manifest with the reason), events and points not
  scored, markets with no price at a point, and forecasts left out for want of a final result.

### Statistics

Every interval is a 95% percentile bootstrap that resamples whole events, not markets
([bootstrap.py](../src/pmfa/evaluation/bootstrap.py)): 2,000 resamples, seeded with
`evaluation.seed` (20261124), so the same rows give the same interval. Markets in one event resolve
on the same number (five per Fed meeting, twenty or more per CPI release), so resampling markets
would treat correlated rows as independent and overstate the sample. Reports show rows, markets and
events side by side. With fewer than two events there is nothing to resample between, and the bounds
are reported as not defined.

The intervals are wide because the window holds few events: 7 so far, four CPI events and three Fed
events. No KXCPIYOY event exists for the October 2026 CPI release (2026-11-10): the exchange lists
none, so that release has nothing to score. Each report lists such releases under "Left out of
scoring". Forecasts of one event at 7, 3 and 1 days are not independent either; the pooled row keeps
an event's rows together across horizons, the per-horizon rows cannot. No difference on this sample
separates skill from luck. The harness is the deliverable; its numbers are a pilot.

### Leakage controls

| Control | Where |
| --- | --- |
| The as-of rule is inside the ports. The snapshot exchange shows quotes, listings and results only up to the simulated clock; the frozen evidence port returns only documents published at or before the as-of time; every retrieval index filters on publish or resolution time inside the Chroma query, before the nearest-neighbour search. | [snapshot/reader.py](../src/pmfa/world/snapshot/reader.py), [evidence/corpus.py](../src/pmfa/world/evidence/corpus.py), [knowledge/index.py](../src/pmfa/knowledge/index.py) |
| A canary document dated one day after the window, containing `evaluation.canary_text` and the words every query uses, sits in the evidence index but not in the corpus. A guard on the knowledge port raises `LeakageDetected` the moment any retrieval returns it, and every finished episode is scanned for it. One hit aborts the run and marks it aborted. The manifest's `leakage_checks` counts the retrievals and episodes checked, and the report says so; a market-only run builds no index, so its canary is not exercised. Dated after every as-of time, the canary catches a missing as-of filter, not one that lets documents through hours or days late. | `CanaryGuard`, `assert_episode_clean` in [world/backtest.py](../src/pmfa/world/backtest.py) |
| Every decision reads the order book as of the as-of time; only the paper fill is priced from a later candle, and no role or decision sees that book (see "Fill assumptions and their limits" below). The order's outcome, filled or not, has a time: the end of the candle that priced it, or, when no candle was found, an hour after the arrival time (the end of the hour searched), or the arrival time when the market had closed by then (see "Fill assumptions and their limits"). The fills are stamped with it and the order carries it as `outcome_at`. The paper account's balance and positions are read as of the clock, each equity point values the account as of its own time, and the stops that read orders (fill failures, a changed source) count an order only from its outcome time on. So no decision made before that time sees the order's price or outcome, not even a second episode at the same clock time. | [snapshot/book.py](../src/pmfa/world/snapshot/book.py), [execution/paper.py](../src/pmfa/execution/paper.py), [execution/halt.py](../src/pmfa/execution/halt.py) |
| In the trading pass an episode starts at the latest of its trigger's time, the clock's time and the latest stored order outcome. An episode that starts later than its trigger runs with that later time as its as-of time, and the report counts these late starts. The scoring pass keeps its fixed as-of times and relies on the time filter of the row above. | `_trading_pass` in [evaluation/backtest.py](../src/pmfa/evaluation/backtest.py) |
| No web access: the backtest world's exchange is the snapshot, its evidence is the stored corpus, and no port holds an HTTP client. | `build_backtest_world` in [world/backtest.py](../src/pmfa/world/backtest.py) |
| The response cache key includes the as-of time, which every role input carries, so a question asked at a later time is a different entry. | [models/cache.py](../src/pmfa/models/cache.py) |
| The knowledge-cutoff guard described above. | [models/pricing.py](../src/pmfa/models/pricing.py), called first in [evaluation/backtest.py](../src/pmfa/evaluation/backtest.py) |

### Cost controls

- **Dry run.** `--dry-run` writes the manifest with the plan and prints model calls and dollars per
  system, with its token assumptions, and calls no model
  ([estimate.py](../src/pmfa/evaluation/estimate.py)). A call is priced from the mean recorded cost
  of the same task on the same model in the response cache when there is one, and from assumed
  tokens times the catalog price otherwise (baseline calls always use the assumptions, because the
  cache does not record whether an answer had passages). The floor assumes every pipeline episode
  stops after its first call; the ceiling bounds every call an episode can make, with each
  evidence follow-up's re-score priced on the critic slot of its level. When the ceiling exceeds the
  run's cap, the plan says the run would stop partial.
- **Dollar cap per run.** The gateway's run meter enforces `budget.max_usd_per_run` (default $100)
  before every call ([meter.py](../src/pmfa/models/meter.py)), against the call's worst-case cost,
  so a call that could take the run past the cap is not sent and the cap is a ceiling. When it trips, the run stops cleanly,
  is marked partial, and writes everything it ran. Each system's status then says how far it got:
  `run` when it finished everything planned, `partial: k of n points` (and `j of m triggers` for the
  pipeline) when it stopped part way, `not run: ...` when the cap came before its first forecast.
  The report marks the rows of a system scored on fewer points, and shows no P&L when the trading
  pass ran no trigger. `--cap` changes it for one run.
- **Response cache.** Validated answers are stored on disk by content under `data/cache/models`, so
  rerunning the same questions costs nothing.
- **Subsample first.** `--limit N` keeps an evenly spaced subsample of N scoring points and N
  triggers; the manifest lists what was left out.
- **Per-episode caps**: 30 model calls and $2.00 per episode.

### Fill assumptions and their limits

A backtest order meets two books ([build_backtest_world](../src/pmfa/world/backtest.py)):

- **The decision book** is the snapshot's book as of the as-of time: one level per side from the
  latest candle that ended at or before it. Every decision reads it: sizing, the price re-read
  before sending, the recompute when nothing is offered at the limit, the gate, and routing's price
  move. Nothing priced after the as-of time reaches a decision.
- **The fill book** is what the order actually fills against, built from one candle after the
  order is sent ([world/snapshot/book.py](../src/pmfa/world/snapshot/book.py)). No decision sees it,
  so a limit that this later candle does not meet leaves the order unfilled.

The fill book:

- An order arrives 60 seconds after the as-of time (`execution.backtest_latency_seconds`).
- It meets the one-minute candle containing the arrival time, at the worst touch of that period:
  buying YES pays the period's highest YES ask, buying NO pays 1 minus its lowest YES bid.
- Each level holds 10% of that candle's volume (`execution.backtest_volume_share`), floored to whole
  contracts. Orders are immediate-or-cancel; the unfilled remainder is dropped, and taker fees are
  paid on every fill.
- The liquidity floor is a proxy: dollars traded in the trailing 60 minutes instead of dollars
  resting near the touch.
- The fill book is dated at the end of the candle that priced it: up to a minute after the arrival
  time on a one-minute candle, up to an hour on the hourly fallback. When no candle is found it is
  dated an hour after the arrival time, the end of the hour searched, and when the market had closed
  by the arrival time, at the arrival time. That date is the order's outcome time: the paper
  account, the equity points and the stops that read orders (fill failures, a changed source) count
  the order only from then (see "Leakage controls").
- In the trading pass the next episode does not start before that time, so an episode can start
  later than its trigger, with the later time as its as-of time. `metrics.json` counts these late
  starts and the report prints the count in "How each fill was priced". The scoring pass keeps its
  fixed as-of times and relies on the time filter.

The limits: Kalshi's history has no order-book depth, so liquidity is inferred from candle volume.
One-minute candles exist only for minutes in which a quote or trade changed, so they are sparse; when
none contains the arrival time, the hourly candle is used and the fill is marked coarse. The book is
empty and nothing fills when neither candle holds the arrival time, when the market has closed by
the arrival time, or when the candle's volume times `execution.backtest_volume_share` is under one
contract. A side is empty when the candle has no quote to price it from: buying YES needs a YES ask
below 1, buying NO a YES bid above 0. Each report counts how every order was priced. Simulated P&L
is not evidence of tradable edge. With no human present, approve-tier proposals are filled and
tagged, and reported apart from auto-tier ones.

What the sparse candles mean for a trading simulation was measured with
[scripts/fill_coverage.py](../scripts/fill_coverage.py) on the 2026-11-25 data. At the 55 news
triggers of the trading pass there are 4,086 open market sides; 204 of them (5%) pass the $500
liquidity floor (`caps.liquidity_floor_dollars`). Of those 204, 118 (58%) had contracts at any price
on their side in the candle an order would meet and 86 (42%) had none. Of the 118, 73 (36% of the
204) had contracts at or below the as-of ask. The script counts contracts at any price for the first
count, and an order whose limit does not meet the candle's touch still fills nothing. In a backtest
the book a decision reads has one level per side, so an order's limit is that as-of ask snapped up
to a valid tick, and the second count is the closer one: 131 of the 204 (64%) had nothing at or
below it. The candle an order would meet was a one-minute candle 129 times, the hourly fallback 73
times, and missing twice. 35 of the 55 triggers had at least one liquid side with contracts to fill
against. The script uses the triggers' own times, so a start delayed by an earlier order's outcome
is not modelled.

A backtest applies the same stops as the live modes
([execution/halt.py](../src/pmfa/execution/halt.py)), and both passes start with an equity point at
the window's start, the bankroll the stops measure from. When the pipeline runs, both passes settle
outcomes as the clock passes them (the scoring pass before each scoring point, the trading pass
before each episode), so the stops read settled results in both. Two of the stops are likely to
trip early: the fill-failure stop in the trading pass, and the calibration stop in either pass.

The fill-failure stop trips when more than `oversight.fill_failure_halt_fraction` (0.10) of the last
`oversight.fill_failure_window` (10) orders did not fill, that is two or more in ten, once ten
orders exist. With 42% of the liquid sides above having no contracts at any price in the candle an
order would meet, and 64% none at or below the as-of ask, the trading simulation is likely to halt
itself on fill failures early. No backtest with model forecasts has been run yet (the fixed-rule
answers of the [rehearsal](#running-the-systems-that-need-a-model) say nothing about this), so this
is an expectation from the data, not an observed result.

The calibration stop trips for a category when the expected calibration error of the pipeline's
resolved ledger entries is above `routing.calibration_error_max` (0.10) at each of three weekly
checkpoints (now, one and two weeks before; `oversight.calibration_halt_weeks` = 2), each holding at
least `routing.calibration_min_resolved` (30) entries (`calibration` in
[state/store.py](../src/pmfa/state/store.py)). The ledger holds one entry per market per forecast,
so one CPI release (21 or 26 markets) forecast twice already passes 30. The window's first CPI
release, June 2026 (`KXCPIYOY-26JUN`, closed 2026-07-14), is the one that surprised the market: the
market's own prices on its 63 scored rows have an expected calibration error of 0.140, against
0.0559 over all 312 rows (computed from the published `forecasts.csv` with ten equal bins, as
[metrics.py](../src/pmfa/evaluation/metrics.py) does). An agent whose forecasts of that release are
no better calibrated than the market's would trip the calibration stop about two weeks after it,
near the end of July, in the scoring pass and in the trading pass alike, and neither would place an
order for the rest of the window. This too is an expectation from the data, not an observed result.

Nobody resumes a halt in a backtest: once a stop trips, the later episodes of that pass still run,
and every one that reaches the gate has already forecast and spent model calls, ends `halted` there
and sends no order. An episode that exits earlier, such as `no_match` or `no_edge`,
never reads the halt and keeps that exit; `self_halts` in `metrics.json` counts how many ended
halted from then on. The report states each pass's halt under "Exits by reason", and under
"Simulated trading after fees" says that the trading figures describe the run only up to the halt.
A halt in the scoring pass stops only its orders: each forecast is made before the gate, so every
scoring point is still forecast and scored. `pmfa backtest --no-trade` runs the scoring pass alone.
Setting `oversight.fill_failure_window = 0` in the configuration a run reads turns the fill-failure
stop off; `oversight.calibration_halt_weeks` and `routing.calibration_error_max` are the settings
behind the calibration stop (the second also decides the auto tier). Whether these stops should apply
inside a backtest is an open question.

### What a run writes and what is committed

`pmfa backtest` writes a run directory under `runs/` (ignored by git), named `<date>-<run name>`:
`manifest.json` (configuration, model ids, effort levels and listed cutoffs, window, commit and,
when the working tree was dirty, the changed and untracked paths, snapshot and corpus manifests, the
snapshot's content digest (`snapshot_content_sha256`) and file hash (`snapshot_sha256`), a digest of
the corpus content hashes, plan, progress, usage, the canary checks, and the leakage, cost and fill
statements), `forecasts.csv` (one row per system, market and as-of time, with the outcome, the
market price and any abstention note), `trades.csv`, `episodes.jsonl` (the run's episodes in the
order it made them, the scoring pass first), `metrics.json`, and `traces/` (up to three full
pipeline episodes saved as replayable examples, each checked by replaying it). A saved trace holds
the store's starting state as of its episode's as-of time and nothing dated later ([decision
0043](decisions/0043-trace-starting-state.md)). Besides the scores, `metrics.json` holds each
system's `error_concentration` (its summed squared error by price band, largest event and three
largest markets, which the report prints as "Where the error comes from"), `self_halts` (for the
scoring and the trading pass, null or the first halted episode with its trigger, start time and
reason, and how many episodes ended halted from then on), and, when a trading pass ran,
`late_starts` (how many trading episodes started later than their trigger, of how many, and the
longest wait in seconds). `pmfa report RUN_DIR` adds `report.md` and `figures/` from the manifest
and metrics alone ([report.py](../src/pmfa/evaluation/report.py)). When the scoring pass ran
pipeline episodes, the report says above its first per-pass table, in "Routing tiers", that the
pass's points follow the calendar and never wait for an earlier order's outcome, so its tiers and
its exits after the forecast step say little about trading. `--publish NAME` copies `manifest.json`,
`forecasts.csv`, `metrics.json`, `report.md`, `figures/` and `traces/` to `evaluation/runs/NAME/`,
and the published manifest's `outputs` lists exactly those files. It refuses a run whose models were
supplied by the caller (scripted answers in a test, or a rehearsal): it prints `not published: ...`,
exits 1 and copies nothing to `evaluation/runs/` (the report is still built in the run directory).
`trades.csv` and `episodes.jsonl` stay in the local run directory. With `--scores-only`, the
per-forecast table and the traces stay there too: they hold exchange prices, which Kalshi's
Developer Agreement does not allow to be shared without permission
([Sources and credits](#sources-and-credits)), so only the manifest, the metrics, the report and its
figures are published, and the published manifest's notes say so. The owner chose scores only for
every run with model forecasts (2026-11-27). A run made from a working tree with uncommitted changes
says so in its report, in the published manifest's notes and in a warning when it is published: its
commit does not hold exactly the code that produced it.

## Results

### The market baseline (2026-11-26)

The only published run is
[evaluation/runs/2026-11-26-market-baseline/](../evaluation/runs/2026-11-26-market-baseline/report.md).
No Anthropic API key was available, so it requested the market alone (`systems_requested` in its
manifest) and its report lists the other systems as "not requested". It made no model call and
spent $0.

| Horizon | Forecasts | Markets | Events | Market Brier score | 95% interval |
| --- | --- | --- | --- | --- | --- |
| all | 312 | 104 | 7 | 0.0345 | 0.0107 to 0.0775 |
| 7 days | 104 | 104 | 7 | 0.0376 | 0.0116 to 0.0799 |
| 3 days | 104 | 104 | 7 | 0.0330 | 0.0091 to 0.0759 |
| 1 day | 104 | 104 | 7 | 0.0329 | 0.0095 to 0.0760 |

What these numbers mean:

- They describe the market, not the agent. They are the bar: a system has positive skill only if
  its Brier score on the same rows is below these. The market's skill against itself is 0 by
  definition.
- The scores are low because most rows are priced near 0 or 1: 185 of 312 prices sat below 0.10 and
  82 above 0.90, mostly far strikes of CPI ladders. Those rows are not all easy. Of the market's
  summed squared error over the 312 rows, 10.7661, the 185 rows priced below 0.10 carry 0.5%, the
  45 rows from 0.10 to 0.90 carry 45.6%, and the 82 rows above 0.90 carry 53.9%. Six of those 82
  rows carry 53.4% on their own: KXCPIYOY-26JUN-T3.5 and T3.6, priced 0.975 to 0.99 at all three
  horizons, both resolved NO. One event, KXCPIYOY-26JUN, carries 69.5% of the error (63 rows), and
  three of its markets carry 26.9% (T3.5), 26.5% (T3.6) and 14.2% (T3.7), three rows each; T3.7 was
  priced 0.705 to 0.725 and also resolved NO. So the market's score on this sample rests mostly on
  one release that surprised it. Since most of the market's error sits in a handful of rows of that
  one event, a system's skill against the market is likely to be decided mostly there. The report
  shows the split under
  [Where the error comes from](../evaluation/runs/2026-11-26-market-baseline/report.md#where-the-error-comes-from).
- The market sharpens slightly toward the close (0.0376 at 7 days, 0.0329 at 1 day), but the
  intervals overlap almost entirely.
- The market's expected calibration error over all horizons is 0.0559.
- 226 of 312 prices came from the last trade, because one side of the quote was empty (mostly far
  strikes with no bid). The standing quote was fresh on most rows (median 0.45 hours since it last
  changed); the last trades were older (median 60.5 hours). 18 of those trades lay outside the
  standing one-sided quote and were clamped to it, which moves the pooled Brier score by less than
  0.000001.

The published run is market-only: it built no retrieval index and made no retrieval
(`leakage_checks` in its [manifest](../evaluation/runs/2026-11-26-market-baseline/manifest.json)),
so the canary guarded nothing in that run; there the as-of rule applied to the snapshot's price
reads. The tests exercise the canary, for example
[tests/evaluation/backtest/test_backtest.py](../tests/evaluation/backtest/test_backtest.py), where a
canary in a retrieval result aborts the run.

### Not yet run

`prompt_only`, `single_pass`, `single_pass_similarity` and `pipeline` have no results, and the
trading simulation has no trades, P&L, routing tiers or pruning audit. Nothing on this page or in the
report describes how they would do.

### Running the systems that need a model

Before any paid run, rehearse it.
[scripts/rehearse_backtest.py](../scripts/rehearse_backtest.py) drives the whole backtest over the
frozen data with a fixed rule in place of every model answer: every system at every scoring point
and the pipeline at every news trigger. It builds the retrieval indexes, sizes and gates orders
against real books, fills them from real candles, applies the stops, saves six traces and replays
each one, and writes a report to `runs/<date>-rehearsal`, which git ignores. It needs no key, costs
nothing and takes about a minute. It exits 0 only when the run completed with no scoring point or
trading trigger left unrun; the pipeline ran in both passes, episodes got past matching in both,
and no pipeline episode ended `rules_mismatch`, `low_coverage` or `error` (a fixed answer refused,
or a step failed); the pipeline made forecasts and sent at least one order; and at least one trace
was saved and every saved trace replays and matches. It prints each failed check as a line starting
`PROBLEM:`. It replaces its run directory, so it refuses, with exit status 1, to write into one
that holds anything other than a run answered by supplied models (such as an earlier rehearsal);
it never replaces a run made with the live gateway or with no models. It does not reach the model
side: the fixed rules stand in for the gateway, so the structured-output schemas, the response
cache, cassette recording, the role runners and the dollar caps are never reached (the per-episode
call cap still applies), and because its critic always scores, critic rejections and evidence
follow-ups never run. Its manifest records `models_source` as `supplied` and starts its notes with
one that begins `REHEARSAL:`, and its report says the models were supplied by the caller and its
numbers are a test of the harness.

```bash
uv run python scripts/rehearse_backtest.py
```

On the 2026-11-25 data it passed: 105 scoring forecasts (21 points for each of 5 systems), 21
pipeline episodes in the scoring pass (executed 2, halted 7, `no_edge` 12) and 55 in the trading
pass (executed 2, halted 8, `no_edge` 30, `no_match` 15), 2 trading orders (neither filled), and six
saved traces that all replay and match. The Critic was given calibration examples in 18 of the 21
scoring episodes. The canary checks covered all 103 retrievals and the 76 finished episodes and
found nothing, and no trading episode started late. Both passes halted themselves on the calibration
stop ("cpi error above 0.1 for 2 weeks"): the scoring pass on 2026-07-28, after which 7 of 16
scoring episodes ended halted, and the trading pass on 2026-07-29, after which 8 of 44 did. This
shows that the pipeline around the models runs end to end on the real data and that a stop trips and
is reported. It shows nothing about forecasting: its answers are fixed rules, so its scores, trades
and halt say nothing about how a model would do. `pmfa report --publish` refuses a run whose models
were supplied by the caller, such as a rehearsal: it prints `not published: ...`, exits 1 and copies
nothing to `evaluation/runs/` (the report is still built in the run directory).

#### The Claude API key

```bash
export ANTHROPIC_API_KEY=...
```

Create the key in the [Claude Console](https://platform.claude.com/settings/keys) and select a
workspace for it there. A key that is not scoped to one workspace is refused unless each request
names its workspace; for such a key, also set `ANTHROPIC_WORKSPACE_ID`. In a Claude Code cloud
session a variable named `ANTHROPIC_API_KEY` is not passed to commands: save the key there under
another name, for example `PMFA_ANTHROPIC_API_KEY`, and name it in `models.api_key_env`.

#### The runs, in order

With `ANTHROPIC_API_KEY` set and the data built, in this order.

**1. Dry run.** It calls no model.

```bash
uv run pmfa backtest --dry-run --systems market,prompt_only,single_pass,single_pass_similarity,pipeline
```

On the 2026-11-25 data, without a key, it printed the run directory it wrote the plan to, then:

```text
Window: 2026-07-01 to 2026-11-25T20:10:48.937084+00:00; 21 scoring point(s), 55 trading trigger(s)
Models: none

system                   status                 forecasts   calls (min-max)       USD (min-max)
market                   would run                     21       0-0             0.00-0.00
prompt_only              not run: no API key           21      21-21            5.29-5.29
                          one baseline call per scoring point; a repair retry would add one
single_pass              not run: no API key           21      21-21            5.80-5.80
                          one baseline call per scoring point; a repair retry would add one
single_pass_similarity   not run: no API key           21      21-21            5.80-5.80
                          one baseline call per scoring point; a repair retry would add one
pipeline                 not run: no API key           76      76-1683          0.04-85.99
                          scheduled episodes by tree depth: depth 1: 18, depth 2: 3
                          55 news-triggered episode(s), each bounded with a full depth-3 tree after one Monitor call
                          the maximum assumes no repair retries: an unusable answer gets one retry, which could add up to as many calls again within each episode's call cap; the run's dollar cap ($100) is the real ceiling
                          the floor assumes every episode stops after its first call (no candidate event, or no evidence)

Estimated total: $16.93 to $102.87 (run cap $100)
The maximum exceeds the run's dollar cap of $100: if the run spends that much it stops at the cap and is marked partial, with what it did not run listed.
Assumptions:
  - Prices are the catalog's dollars per million tokens (config.models.catalog).
  - baseline on claude-fable-5-2: assumed 1500 input and 6000 output tokens, $0.2520 per call
  - baseline_with_passages on claude-fable-5-2: assumed 4500 input and 6000 output tokens, $0.2760 per call
  - generate on claude-haiku-5-5: assumed 5000 input and 2500 output tokens, $0.0018 per call
  - match on claude-haiku-5-5: assumed 3000 input and 600 output tokens, $0.0006 per call
  - plan_queries on claude-haiku-5-5: assumed 1500 input and 800 output tokens, $0.0006 per call
  - score on claude-fable-5-2: assumed 6000 input and 6000 output tokens, $0.2880 per call
  - score on claude-sonnet-5-5: assumed 6000 input and 6000 output tokens, $0.0720 per call
  - select_passages on claude-haiku-5-5: assumed 4500 input and 1200 output tokens, $0.0010 per call
  - Answers already in the response cache cost nothing on a rerun; the estimate does not subtract them.
```

With the default systems (without `single_pass_similarity`) the total is $11.13 to $97.08, inside
the default $100 cap. The estimate uses assumed token counts until the cache holds recorded costs;
the high-effort calls are assumed to write 6,000 output tokens each, thinking included. With all
five systems the upper end exceeds the cap, so a run that spent the maximum would stop there and be
marked partial.

**2. A subsample.** Three scoring points and three triggers; the dry run estimated $1.59 to $7.28 for
the default systems. Its report shows the real cost per call and per episode, and its answers stay in
the cache.

```bash
uv run pmfa backtest --limit 3 --run-name pilot
```

```bash
uv run pmfa report runs/YYYY-MM-DD-pilot
```

**3. The full run**, with a cap chosen from the pilot's measured cost and a fresh dry run (which now
prices calls from the cache). Cached answers from the pilot cost nothing again. Its trading pass is
likely to halt itself on fill failures or on calibration (see "Fill assumptions and their limits");
for a run without the fill-failure stop, pass `--config` a copy of `pmfa.toml` with
`oversight.fill_failure_window = 0`.

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
saved traces; with `--scores-only` it leaves out the per-forecast table and the traces. The notebook
[notebooks/evaluation.ipynb](../notebooks/evaluation.ipynb) reads the newest published run: it
recomputes the scores when the per-forecast table is there, and reads them from `metrics.json` when
the run was published as scores only.

## Limitations

- **The agent's forecasting has not been measured.** No backtest or other evaluation with a model
  has been run here; the only forecasting result measured is the market's own (its Brier score and
  calibration error).
- **The sample is small.** The window from 2026-07-01 holds 7 settled events in the two followed
  series. No result on it can separate skill from luck; the harness is the deliverable and its numbers
  are a pilot.
- **Many backtest prices are old trades**: 226 of the 312 scored prices used the last trade
  ([The market baseline](#the-market-baseline-2026-11-26)).
- **Backtest fills are simulated from candles**, with no order-book depth
  ([Fill assumptions and their limits](#fill-assumptions-and-their-limits)).
- **A backtest's trading simulation is likely to halt itself early**, on fill failures or on
  calibration; the reasons and the numbers are in
  [Fill assumptions and their limits](#fill-assumptions-and-their-limits).
- **Sample episodes are hand-made.** Their model records are fixed text written in advance, with an
  AI coding assistant and with the outcomes known. They are not the output of the pipeline's roles.
- **Exchange-side protections are documented, not verified.** The subaccount restriction, the
  funds-transfer block and the order-group breaker have not been exercised against a live demo
  account.
- **An injected passage can still distort a probability**, within the caps; roles cannot act on it.
- **Evidence is official sources only**: Federal Reserve and BLS. No news wires or web search. A
  generic RSS or Atom feed source exists for the live modes, off by default and secondary only.
- **Stage demotion runs only in the live modes.** Each pass of the shadow or demo loop drops the
  stage one step after daily loss stops on two days of the last seven; backtests and the episode
  pipeline itself do not apply the rule.

## Sources and credits

Evidence text comes from the Board of Governors of the Federal Reserve System
([federalreserve.gov](https://www.federalreserve.gov/disclaimer.htm)) and the U.S. Bureau of Labor
Statistics ([bls.gov](https://www.bls.gov/bls/linksite.htm)). Both agencies place their text in the
public domain and ask to be credited as the source; this project credits them here and in the
README, and each example's README names its sources and URLs. Market data comes from Kalshi's
public API. The exchange data committed here is: the examples' `world.json` files (one event and a
handful of markets per example, one book level per side, no candle series), some of whose prices the
`expected/` outputs repeat; the test fixtures under
[tests/world/kalshi/fixtures/](../tests/world/kalshi/fixtures/) whose `_source` says they were
recorded, API responses cut down to a few items each (the four candle files hold two to seven
candles each), and the events under [tests/forecast/fixtures/](../tests/forecast/fixtures/), cut
down to a few markets each; and the published run's `forecasts.csv` (312 rows: the market price of
104 markets in 7 events at three horizons, each with its outcome), `manifest.json`, which names the
12 events the snapshot builder did not store, with their close times, and `metrics.json`,
`report.md` and `figures/`, which summarize those rows (scores by horizon, sample sizes by event and
market, reliability, where the error comes from) and draw them as Brier scores by horizon, a
reliability diagram and an equity curve, which is empty because the run did not trade. The fixtures
whose `_source` says CONSTRUCTED were built by hand from Kalshi's API documentation and hold no
recorded exchange data. The snapshot with its candle series and the corpus are not committed: they
are rebuilt locally and ignored by git. The one synthetic story in `single-origin-syndicated` uses
made-up outlets on reserved `.example` domains.

**The exchange data here is not cleared for reuse.** Kalshi's API documentation says that using the
API binds the user to its [Developer Agreement](https://kalshi.com/developer-agreement). The copy
read for this project on 2026-11-27 (v1.1, from Kalshi's public documents; the linked page refused
an automated request, so check it for a newer version) limits use of the API to "facilitating a
members own trading on the Exchange". It prohibits "collecting, caching, aggregating, or storing
data or content accessed via the API except for purposes of facilitating your own trading on
Kalshi", and says "You may not share such data or content with third parties in any manner without
prior written authorization from Kalshi." No authorization has been asked for or given for this
project. Whether the committed exchange data and the local snapshot may be kept is therefore an
open question that only Kalshi can settle; until it is settled, do not reuse that data.
