# pmfa: a prediction-market forecasting agent

[![CI](https://github.com/youngminpark14/CMU-Capstone-Project/actions/workflows/ci.yml/badge.svg)](https://github.com/youngminpark14/CMU-Capstone-Project/actions/workflows/ci.yml)

## Overview

pmfa is an agent that forecasts Kalshi prediction markets on U.S. economic releases (Federal Reserve
rate decisions and monthly CPI) and sizes positions on them. It is a capstone project and uses mock
funds only. Each trigger starts one *episode* ([glossary](docs/glossary.md)), in which the agent:

- notices a new official release or a price move, and matches it to the event it affects;
- retrieves evidence published before that moment, and nothing published after it;
- forecasts every market of the event: a Generator and an independent Critic search a scenario tree;
- compares the forecast with the market price after fees, and sizes a small order;
- checks the order against hard limits in code (the *gate*);
- routes it (the *routing tier*): sends an immediate-or-cancel limit order, asks a person, or halts;
- records every step and how the episode ended (its *exit*), so any decision can be replayed.

Four Claude model roles (Monitor, Researcher, Generator, Critic) only read and judge: each takes one
record and returns one, and none can call a tool. Code does every retrieval, check and order.

It never trades real money and has no way to place an order on Kalshi's production exchange; its
only orders go to Kalshi's demo exchange, which uses mock funds. It is not trading advice.

## Status

- Built end to end, with an offline suite of more than 1,300 tests (no keys, no network).
- Five sample episodes replay offline and match their committed outputs.
- Only the market's own forecasting score has been measured; no evaluation with a model has run yet.
- Frozen real data, built 2026-11-25: 7 settled events, 104 markets, 45 Fed and 10 BLS documents.
- A read-only check of a live Kalshi demo account passed on 2026-11-27; no order was sent there.

Beating the market price is the hypothesis the evaluation tests, not a result of this project.

## Project structure

```text
src/pmfa/
├── episode/        # one episode as a LangGraph graph: its steps and the trace
├── models/         # the model gateway, cache, cassettes, and the four roles
├── knowledge/      # chunking, embedders, Chroma indexes, as-of retrieval, origins
├── forecast/       # outcome spaces, scenario tree, checks, beam search, aggregation
├── decision/       # fees, sizing, the gate, routing: pure functions, no I/O
├── execution/      # paper and demo executors, review queue, halt, stages
├── world/          # adapters behind the ports: Kalshi, snapshot, evidence, worlds
├── state/          # the SQLite store and its MCP server
├── live/           # triggers and the loop for shadow and demo mode
├── evaluation/     # backtest, baselines, metrics, bootstrap, report
├── records/        # immutable records shared between modules
├── cli/            # the pmfa command
├── config.py       # the one typed configuration
└── ports.py        # the protocols every world implements
tests/              # mirrors src/pmfa/; tests/docs/ checks the documents
docs/               # the pages listed under Documentation below
examples/           # five sample episodes that replay offline
evaluation/runs/    # the published backtest run
scripts/            # backtest rehearsal, fill coverage, example builder, recency example
```

## Where the agent is

1. [`src/pmfa/episode/pipeline.py`](src/pmfa/episode/pipeline.py): one episode's steps, in order.
2. [`episode/graph.py`](src/pmfa/episode/graph.py): which step can follow which.
3. [`models/roles/specs.py`](src/pmfa/models/roles/specs.py): the four roles and what each is asked.
4. [`records/roles.py`](src/pmfa/records/roles.py): the records the roles receive and return.
5. [`knowledge/retriever.py`](src/pmfa/knowledge/retriever.py): retrieval as of a time.
6. [`forecast/engine.py`](src/pmfa/forecast/engine.py): the beam search over the scenario tree.
7. [`decision/sizing.py`](src/pmfa/decision/sizing.py): the edge after fees, and the size.
8. [`decision/gate.py`](src/pmfa/decision/gate.py): every hard limit, checked again in code.
9. [`decision/routing.py`](src/pmfa/decision/routing.py): auto, approve or halt.
10. [`tests/episodes/test_scenarios.py`](tests/episodes/test_scenarios.py): the spec's 16 scenarios.

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/), then `uv sync`. A second, optional dependency set
uses the MCP SDK 2.x without CrewAI ([decision 0013](docs/decisions/0013-two-dependency-sets.md)).

## Run

```bash
uv run pmfa replay fed-minutes-hawkish   # replay one sample episode: no key, no network
```

It prints the trace, forecast, proposal, gate checks, routing tier, simulated order and exit. The
five examples' model records were written in advance with an AI coding assistant, not by the roles:

- [fed-minutes-hawkish](examples/episodes/fed-minutes-hawkish/): hawkish Fed minutes, an approved NO order; exit `executed`.
- [phantom-edge-no-trade](examples/episodes/phantom-edge-no-trade/): a hot CPI report already priced; exit `no_edge`.
- [review-resize](examples/episodes/review-resize/): a reviewer shrinks the order, the gate runs again, and it fills; exit `executed`.
- [cpi-ladder](examples/episodes/cpi-ladder/): monotone probabilities across CPI strikes, none liquid; exit `no_edge`.
- [single-origin-syndicated](examples/episodes/single-origin-syndicated/): one invented story syndicated twice; exit `insufficient_origins`.

Shadow mode, demo mode and the backtest's model systems need a Claude API key; without one,
`pmfa backtest` scores the market alone. The first three steps below need no key. Their order and
cost are in [docs/evaluation.md](docs/evaluation.md#running-the-systems-that-need-a-model).

```bash
uv run pmfa data build                       # rebuild the frozen data
uv run python scripts/rehearse_backtest.py   # rehearse the whole backtest with fixed answers
uv run pmfa backtest --dry-run               # print the plan and its cost; calls no model
uv run pmfa backtest                         # run the backtest
uv run pmfa shadow                           # one pass on live prices; trades only on paper
uv run pmfa demo                             # demo exchange: each order waits for approval
```

Every demo order waits in `pmfa review list --mode demo`. Demo P&L is never evidence: Kalshi warns
that demo prices may not reflect real markets. `uv run pmfa --help` lists every command.

## Environment variables

- `ANTHROPIC_API_KEY`: the backtest's model systems, shadow and demo mode.
- `KALSHI_DEMO_KEY_ID`, and `KALSHI_DEMO_PRIVATE_KEY` or `KALSHI_DEMO_PRIVATE_KEY_PATH`: demo only.

Nothing is needed for replay and the tests. Export each variable you use in the shell that runs
`pmfa`, for example `export ANTHROPIC_API_KEY=...`. Nothing reads a `.env` file. Never commit a
key. [.env.example](.env.example) lists every variable. A key not scoped to one workspace, and a
Claude Code cloud session, each need [one more step](docs/evaluation.md#the-claude-api-key).

## What an episode produces

- the trace: numbered thought, action and observation steps (thoughts are what a model wrote);
- the forecast: a probability for every market of the event;
- the proposal, with every check of the gate and the routing tier (auto, approve or halt);
- the order and its fill, and the named exit, such as `executed` or `no_edge`.

A backtest writes `forecasts.csv`, `metrics.json` and other files; `pmfa report` adds `report.md`.
See the [published run](evaluation/runs/2026-11-26-market-baseline/report.md).

## Results

One backtest has been run and published. It scored only the market's own price, 7, 3 and 1 days
before each event's close: not the agent's numbers, but the bar a later system has to clear. Seven
events are a small sample, so the intervals are wide. The method, the caveats and where the error
comes from are in [docs/evaluation.md](docs/evaluation.md#results).

<!-- results-table:start (tests/docs/test_readme.py checks these numbers against metrics.json) -->
| Horizon | Forecasts | Markets | Events | Market Brier score | 95% interval |
| --- | --- | --- | --- | --- | --- |
| all | 312 | 104 | 7 | 0.0345 | 0.0107 to 0.0775 |
| 7d | 104 | 104 | 7 | 0.0376 | 0.0116 to 0.0799 |
| 3d | 104 | 104 | 7 | 0.0330 | 0.0091 to 0.0759 |
| 1d | 104 | 104 | 7 | 0.0329 | 0.0095 to 0.0760 |
<!-- results-table:end -->

## Tests

`uv run pytest` runs the suite: offline, deterministic, no keys. [CI](.github/workflows/ci.yml) runs
it, with `ruff check` and `ruff format --check`, on every push and pull request.

## Documentation

- [docs/review.md](docs/review.md): a ten-minute review path, with nothing to install.
- [docs/architecture.md](docs/architecture.md): pipeline, roles, run modes, commands, configuration.
- [docs/guardrails.md](docs/guardrails.md): every guardrail, who enforces it, and its test.
- [docs/evaluation.md](docs/evaluation.md): the full status, method, results, limitations.
- [docs/checkpoints.md](docs/checkpoints.md): the six checkpoints and feedback, traced to code.
- [docs/decisions/README.md](docs/decisions/README.md): 45 decision records, each with its reasons.
- [docs/glossary.md](docs/glossary.md) and [docs/spec.md](docs/spec.md): the terms and the spec.
- [examples/README.md](examples/README.md): the sample episodes and where their inputs came from.
- [notebooks/README.md](notebooks/README.md): a notebook that rebuilds the report's tables.

## Limitations

- The agent's forecasting has not been measured; only the market's own score has.
- The sample is small: 7 settled events cannot separate skill from luck.
- Backtest fills are simulated from candles, and many backtest prices are old trades.
- Evidence is official sources only: the Federal Reserve and the BLS.
- Sample episodes are hand-made: their model records were written in advance, with outcomes known.
- The exchange-side protections are documented, not verified against a live account.
- The full list is in [docs/evaluation.md](docs/evaluation.md#limitations).

## Notes

- **Educational use.** A course project on mock funds: not trading or investment advice, and not
  affiliated with Kalshi, the Federal Reserve or the Bureau of Labor Statistics.
- **Sources and the Kalshi notice.** Evidence text is from the Federal Reserve and the BLS, in the
  public domain and credited here. **The exchange data here is not cleared for reuse.** Kalshi's
  [Developer Agreement](https://kalshi.com/developer-agreement) prohibits "collecting, caching,
  aggregating, or storing data or content accessed via the API except for purposes of facilitating
  your own trading on Kalshi", and sharing it "without prior written authorization from Kalshi."
  None has been asked for or given ([full statement](docs/evaluation.md#sources-and-credits)).
- **License.** MIT ([LICENSE](LICENSE)) for the code and documents, not for Kalshi's market data.
- **Acknowledgment and AI assistance.** Built as the capstone project for Carnegie Mellon
  University's Agentic AI Program: Building Autonomous Systems for Real-World Applications, from
  its six checkpoints and the instructors' feedback ([traced to the code](docs/checkpoints.md)).
  This repository was built with [Claude Code](https://claude.com/claude-code), Anthropic's AI
  coding assistant, which wrote its code, tests and documents; the commits say so.
