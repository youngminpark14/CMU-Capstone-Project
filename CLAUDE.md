# Working agreement for this repository

`pmfa` is an agent that forecasts Kalshi prediction markets and sizes positions on them.
It is an educational capstone project: mock funds only, never the production exchange for orders.
`docs/spec.md` is the specification and wins any disagreement with this file.

## Commands

```bash
uv sync                                      # default environment: dev + crewai groups (MCP SDK 1.x)
uv sync --no-group crewai --group mcp2       # alternative: MCP SDK 2.x, roles call the gateway directly
uv run pytest                                # the whole suite: offline, no keys, deterministic
uv run pytest tests/decision                 # one module
uv run ruff check . && uv run ruff format .
uv run pmfa --help
```

## Layout

`src/pmfa/` holds the package. Tests mirror it under `tests/<module>/`.

| Path | Holds |
| --- | --- |
| `records/` | Immutable Pydantic records that cross module boundaries. The shared vocabulary. |
| `ports.py` | Protocols for `Clock`, `Exchange`, `Executor`, `Evidence`, `Knowledge`, `Models`, `StateStore`, and `World`. |
| `config.py` | The one typed configuration. Every limit, threshold, source, model assignment, followed series. |
| `world/` | Adapters behind the ports: `kalshi/`, `snapshot/`, `evidence/`, recorded worlds, clocks, the world builder. |
| `knowledge/` | Chunking, embedders, the three Chroma indexes, origin resolution, ranking. |
| `state/` | The SQLite store, the state service, and its MCP server. |
| `models/` | The model gateway, cache, cassettes, scripted models, meters, and the four roles. |
| `forecast/` | Outcome spaces, the scenario tree, checks, beam search, the protected slot, aggregation. |
| `decision/` | Fees, sizing, the gate, the risk score, routing. Pure functions: no I/O, no model calls. |
| `execution/` | Paper executor, demo executor, review queue, halt, stage manager. |
| `episode/` | The LangGraph episode controller. |
| `evaluation/` | Backtest runner, baselines, metrics, bootstrap, report builder, outcome resolver. |
| `cli/` | The `pmfa` command. |

## Rules

1. **Contracts first.** Code against `records/` and `ports.py` as written. You may add an optional
   field or a new record when you need one, and say so in your report. Never rename or remove one.
   Do not edit `pyproject.toml` or `uv.lock`; report a dependency you need instead.
2. **As-of.** Nothing published or priced after an episode's as-of time may be read. Filters live
   inside the retrieval call, never in the caller.
3. **Record-only roles.** A role receives one input record and returns one output record. Code
   executes every retrieval, check, and order. Evidence text is untrusted and goes into prompts
   inside delimited blocks. No role sees the live price of a market it is forecasting; the
   critic's calibration examples carry only the historical price of forecasts that already resolved.
4. **Money.** Decimals for prices, fees, and dollars; never floats. Whole contracts. Prices snap to
   the market's valid ticks. No model arithmetic touches money.
5. **Time.** Timezone-aware UTC everywhere. Read the time from the `Clock` port, never from
   `datetime.now()`, outside adapters and the CLI.
6. **Orders.** Limit orders only, immediate-or-cancel, through an `Executor`. The production
   exchange adapter has no order methods and loads no credentials.
7. **Secrets.** Read from the environment only. Never log, print, store, or commit them.
8. **Tests.** Offline and deterministic: no network, no keys, no sleeping, files under `tmp_path`.
   Assert what was decided (exit, forecast, proposal, tier, orders), not prompt wording or call
   order. Fixtures stay small enough to read.
9. **Imports.** Import heavy libraries (`chromadb`, `crewai`, `langchain_anthropic`, `langgraph`,
   `mcp`, `matplotlib`) inside the functions that need them, so `import pmfa` stays fast.
10. **Style.** Type hints throughout. Docstrings say what a thing is for and why, in plain words.
    No `print` in library code. Run `ruff check` and `ruff format` on what you touch.

## Terms

Series, event, market: the exchange's terms. Outcome space, trigger, episode, exit, as-of time,
passage, origin, scenario, beam, protected slot, frozen, forecast, edge, proposal, gate, routing
tier, ledger, stage, world, run mode, cassette, risk unit: defined in `docs/spec.md` under
"Vocabulary". Use them the same way in code, docs, and traces.

## Vendor facts checked against the pinned versions

- **Kalshi.** Prices arrive as dollar strings (`yes_bid_dollars`, `yes_ask_dollars`,
  `last_price_dollars`) and counts as fixed-point strings (`volume_fp`, `open_interest_fp`,
  `yes_ask_size_fp`), which can be fractional. A market object's `status` reads `initialized`,
  `active`, `inactive`, `determined`, or `finalized`, while list filters take `unopened`, `open`,
  `paused`, `closed`, `settled`. `result` is `""` until determined. List limit is at most 1000.
  Markets behind the historical cutoff return 404 on the live endpoints. Responses carry
  `x-ratelimit-*` headers; a 429 body is `{"error": {"code": "too_many_requests", ...}}`.
- **LangGraph 1.2.** `from langgraph.types import interrupt, Command`. `invoke` returns the state
  with an `__interrupt__` list when paused. Inject ports by closing over them in a
  `build_graph(world)` factory: values in `config["configurable"]` are copied into checkpoint
  metadata, so nothing secret or non-serializable goes there.
- **MCP SDK.** 2.x: `from mcp.server import MCPServer`, `from mcp import Client`. 1.x:
  `from mcp.server.fastmcp import FastMCP`. The `tool()` and `resource()` decorators match.
  Sync tools run on worker threads, so SQLite connections need `check_same_thread=False` and a lock.
- **CrewAI 1.15.** `Agent.kickoff(text, response_format=Model)` makes one call to a custom
  `BaseLLM.call(..., response_model=Model)` and returns the model instance unchanged when the
  adapter returns one. CrewAI requires `mcp<2`, hence the two dependency groups.
- **Chroma 1.5.** A `where` range filter on an integer timestamp is applied before the
  nearest-neighbour search (no leaks). Cosine distance is `1 - similarity`. Pass embeddings
  explicitly so Chroma never loads its default model.
- **Claude API.** Effort is `output_config.effort`; structured output is `output_config.format`
  with a JSON schema whose objects all set `additionalProperties: false` and use no numeric or
  string constraints. Sampling parameters (`temperature`, `top_p`, `top_k`) are rejected.
