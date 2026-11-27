# Spec: Prediction-Market Forecasting Agent (capstone repository, v1)

Status: ready-for-agent

This spec turns capstone checkpoints 1.1–6.1 and their instructor feedback into one buildable project. Vendor facts in it were checked against current documentation on 2026-11-24 and are listed under Further Notes.

## Problem Statement

I have six design checkpoints for an agent that forecasts Kalshi prediction markets and sizes positions on them, and no working system. My capstone report has to link a public GitHub repository that lets a technical reader understand, run, and review the project. At minimum that means a README covering the project, architecture, setup, and usage; the main code; sample inputs, outputs, and evaluation artifacts; and clear instructions for running or reviewing it.

Code alone will not satisfy that reader, for two reasons:

- **Nothing has been measured.** Each checkpoint argued that retrieval, a scenario-tree search, separated roles, and code-enforced limits make the agent more reliable than a single prompt. None of those claims has a number behind it.
- **The safety properties are only described.** The design says no model can move money and that I approve trades until a record justifies autonomy. A reviewer should be able to see those properties hold in a trace and a test, not take them on trust.

The original problem also stands for me as the trader the agent serves: when news breaks, I cannot consistently check a market's exact rules, gather dated evidence, estimate a probability, compare it with the live price, and size a position against a bankroll, so I end up sizing by instinct.

## Solution

A Python repository that implements the agent end to end and can be reviewed at three depths:

1. **Read (ten minutes, nothing to install).** A README with an architecture diagram, the guardrail model, a results summary, and links to committed sample episodes and an evaluation report.
2. **Replay (one command, no accounts or keys).** Recorded episodes run offline and print the full trace from trigger to decision.
3. **Reproduce (Anthropic key; Kalshi demo key optional).** Rebuild the data snapshot, rerun the backtest, run shadow mode on live data, and place approved orders on Kalshi's demo exchange with mock funds.

One episode pipeline serves four run modes that differ only in what sits behind its ports: **replay** (recorded world and recorded model responses), **backtest** (a frozen snapshot read as of each forecast time, real models), **shadow** (live data, simulated fills, no orders), and **demo** (Kalshi's demo exchange, human approval before every order).

The pipeline is the design from the checkpoints: match a trigger to the event it affects, retrieve the event's rules and dated evidence, search a scenario tree with a generator and an independent critic, size with a fee-aware fractional-Kelly calculator, pass a code-enforced risk gate, route by risk score to automatic execution or human review, place limit orders, and record everything to a ledger that can replay any decision.

Success for v1 is a working, reviewable system with a truthful evaluation. Beating the market price is the hypothesis the evaluation tests, not a deliverable. The report states what was measured, including where the agent does worse than the market.

## User Stories

**Reviewer (instructor, engineer, or recruiter reading the repository)**

1. As a reviewer, I want a README that states what the agent does, what it does not do, and its current status, so that I can decide in two minutes whether to read on.
2. As a reviewer, I want an architecture diagram that renders on GitHub and shows the episode pipeline, the four LLM roles, the code-only roles, and the state server, so that I can see where reasoning ends and enforcement begins.
3. As a reviewer, I want a "review in ten minutes" path that names the few things to open, so that I can assess the project without installing anything.
4. As a reviewer, I want to run a recorded episode offline with one command and no API keys, so that I can watch the system work without accounts or cost.
5. As a reviewer, I want each sample episode committed as input (trigger, event snapshot, evidence passages) and output (trace, forecast, decision), so that I can check the reasoning against its sources.
6. As a reviewer, I want every forecast in a trace to cite passage ids with source, URL, and publish time, so that I can verify a claim against its origin and date.
7. As a reviewer, I want a committed evaluation report with the run's configuration, sample sizes, metrics, and limitations, so that I can judge the results without rerunning them.
8. As a reviewer, I want the evaluation to compare the agent with the market price and with a prompt-only baseline, so that I can see what the architecture adds.
9. As a reviewer, I want the report to state how look-ahead was prevented, so that I can trust that the backtest did not see the future.
10. As a reviewer, I want the guardrails listed with where each is enforced (code, exchange, or human), so that I can audit the safety claims.
11. As a reviewer, I want tests that run offline in CI with a visible status badge, so that I know the repository works as cloned.
12. As a reviewer, I want setup instructions that separate the "no keys", "Anthropic key", and "Kalshi demo key" paths, so that I can go as deep as I choose.
13. As a reviewer, I want design decisions recorded with their reasons and the checkpoint they came from, so that I can follow how the design evolved.
14. As a reviewer, I want a glossary of the project's terms, so that the code, docs, and traces use one vocabulary.
15. As a reviewer, I want a clear statement that this is an educational project running on mock funds, so that nobody mistakes it for trading advice or a production system.

**Trader (the person the agent works for)**

16. As the trader, I want the agent to notice a new official release or a price move and match it to the event it affects, so that I do not have to watch feeds.
17. As the trader, I want the agent to read the event's exact rules before forecasting, so that it forecasts the question the market actually asks.
18. As the trader, I want evidence limited to what was published before the forecast time and ranked with recency and source independence, so that stale or syndicated text does not inflate confidence.
19. As the trader, I want one coherent forecast per event that yields a probability for each of its markets, so that strikes on the same ladder never contradict each other.
20. As the trader, I want the forecast built from alternative scenarios scored by a critic that cannot see the generator's reasoning, so that agreement means something.
21. As the trader, I want unlikely scenarios kept in the forecast, and examined further when they would change the trade, so that tail risk is sized instead of ignored.
22. As the trader, I want position size computed in code from the fee-adjusted edge and my bankroll, so that no model arithmetic touches money.
23. As the trader, I want hard caps per order, per market, per event, and per day enforced by a gate that no prompt can loosen, so that a bad forecast cannot become a large loss.
24. As the trader, I want trades the agent is unsure about routed to me with a short review card and three choices, so that my attention goes where it matters.
25. As the trader, I want to approve, resize, or reject with a reason code, so that my overrides become data.
26. As the trader, I want a pending proposal to expire or be rechecked when the price moves past its edge, so that I never approve a stale trade.
27. As the trader, I want only limit orders, placed through a key restricted to one subaccount, so that the worst case is bounded by the exchange as well as by my code.
28. As the trader, I want one halt command that cancels open orders and blocks new ones, so that I can stop everything immediately.
29. As the trader, I want the agent to halt itself on a daily loss stop, a drawdown limit, repeated fill failures, or a changed source behind an open position, so that it stops when it should even if I am away.
30. As the trader, I want open positions re-forecast when new evidence arrives and only the difference traded, so that buying and selling follow the same loop.
31. As the trader, I want shadow mode to log the trades the agent would make on live prices without placing them, so that I can build a record before risking anything.
32. As the trader, I want an autonomy stage that advances only when recorded benchmarks are met and drops back after repeated loss stops, so that independence is earned and revocable.
33. As the trader, I want a blocklist of markets I must not trade, so that I never trade where I might hold non-public information.
34. As the trader, I want a report of P&L, drawdown, rejections, fill quality, overrides, and calibration, so that I can see how the agent is behaving.
35. As the trader, I want each episode capped in model calls, seconds, and dollars, so that cost and latency stay bounded.

**Evaluator (me, measuring whether the design claims hold)**

36. As the evaluator, I want to build a frozen snapshot of settled markets, their price history, and a timestamped evidence corpus, so that backtests are repeatable and never call live services.
37. As the evaluator, I want the backtest window to start after the models' knowledge cutoff, so that no model can remember an outcome.
38. As the evaluator, I want forecasts scored with the Brier score against the market's price at the same moment, so that calibration alone is not mistaken for skill.
39. As the evaluator, I want simulated P&L after fees with stated fill assumptions, so that edge on paper is not mistaken for edge after costs.
40. As the evaluator, I want confidence intervals that treat markets in the same event as one cluster, so that correlated strikes do not overstate the sample.
41. As the evaluator, I want ablations for prompt-only, single-pass, and full-tree forecasting, and for similarity-only against recency- and origin-aware ranking, so that each design claim has a number.
42. As the evaluator, I want a dollar cap and a response cache on every run, so that a backtest cannot exceed my API budget and reruns are free.
43. As the evaluator, I want resolved outcomes written back to the ledger with a pruning audit, so that I learn how often the search stopped expanding the path that happened.
44. As the evaluator, I want every run to write a manifest with configuration, model ids, effort levels, date window, and commit, so that any reported number can be traced to what produced it.

**Maintainer (me or an implementing agent)**

45. As a maintainer, I want every external dependency behind a port with a recorded implementation, so that the whole pipeline is testable offline.
46. As a maintainer, I want all model calls to pass through one gateway that owns model choice, effort, budgets, caching, and schema validation, so that swapping a model or framework is a one-module change.
47. As a maintainer, I want LLM roles to emit validated records and never call tools themselves, so that untrusted evidence text cannot trigger an action.
48. As a maintainer, I want all limits, thresholds, sources, and followed series in one typed configuration, so that behavior changes are reviewable diffs.
49. As a maintainer, I want secrets read only from the environment and never logged or committed, so that the public repository stays safe.
50. As a maintainer, I want a single command-line entry point with one subcommand per run mode and operation, so that usage is discoverable.

## Implementation Decisions

### Vocabulary

These terms are used the same way in code, docs, and traces. They seed the repository's glossary.

- **Series, event, market**: the exchange's terms. A series (monthly CPI) has events (September CPI). An event has one market per option or strike.
- **Outcome space**: the mutually exclusive outcomes of an event, derived from its markets. Options for a categorical event; numeric bins between strikes for a ladder.
- **Trigger**: what starts an episode. A new document, a price move, or a schedule tick.
- **Episode**: one pass of the pipeline for one trigger and one event. It ends in exactly one named **exit**.
- **As-of time**: the moment an episode runs "at". Nothing published or priced after it may be read.
- **Passage**: a chunk of an evidence document with its source, URL, and publish time.
- **Origin**: a publisher's original document after duplicates are merged. A **primary origin** is an allowlisted official publisher for the event's category.
- **Scenario**: a path of assumed outcomes for the scheduled events before resolution, with a running probability and an outcome distribution.
- **Beam**: the scenarios expanded at one depth. The **protected slot** is one extra place for a scenario whose removal would change the decision. Scenarios outside the beam are **frozen**: not expanded further, still counted in the forecast.
- **Forecast**: a probability for every market in the event, a confidence, and the supporting paths.
- **Edge**: forecast probability minus price, after fees.
- **Proposal**: a sized order intent. The **gate** approves or rejects it. The **routing tier** (auto, approve, halt) says who decides.
- **Ledger**: the durable record of forecasts, prices at forecast time, outcomes, scores, and overrides.
- **Stage**: the autonomy level. Backtest, shadow, approval, autonomous.
- **World**: the set of ports an episode sees. A **run mode** is a choice of world.
- **Cassette**: recorded model responses used for offline replay.
- **Risk unit**: 1% of bankroll.

### Stack

- Python 3.12 with `uv` and a committed lockfile. CrewAI supports Python 3.10 through 3.13.
- Pydantic models for every record that crosses a module boundary.
- LangGraph for the episode graph and the beam loop, with a SQLite checkpointer for human-approval pauses.
- LangChain's Anthropic chat model inside the model gateway. It documents both the effort setting and native JSON-schema structured output.
- CrewAI for the four LLM role definitions.
- The MCP Python SDK (v2 line) for the state server.
- Chroma (local, persistent) with a small local sentence-embedding model.
- SQLite for all durable state.
- A thin in-repo REST client for the dozen Kalshi endpoints used. Kalshi's docs warn that its SDKs can lag the API.
- pytest and Hypothesis, ruff, GitHub Actions.

### Architecture: one pipeline, four worlds

An episode touches the outside only through ports: **Exchange** (read series, events, markets, rules, order book, candles, balance, and positions; place and cancel orders), **Evidence** (documents published at or before a time), **Models** (role and input record in, validated record out), **Clock**, and the **State** store. A run mode is a set of port implementations:

| Mode | Exchange | Evidence | Models | Orders |
| --- | --- | --- | --- | --- |
| Replay | Recorded snapshot | Recorded corpus | Cassettes | Simulated |
| Backtest | Frozen snapshot, read as of the forecast time | Frozen corpus, read as of the forecast time | Live, cached | Simulated |
| Shadow | Live production market data (public, read-only) | Live official feeds | Live | Simulated against the live order book |
| Demo | Kalshi demo exchange for data and orders | Live official feeds | Live | Real orders, mock funds, human approval |

Kalshi warns that demo prices may not reflect real markets. Demo mode therefore demonstrates execution plumbing and oversight, and its P&L is never reported as evidence. Trading evidence comes from backtest and shadow. The production adapter is read-only by construction: it has no order methods and loads no credentials.

Modules, each with a small interface:

1. **Episode controller**: the graph. One entry: run an episode for a trigger and return its record.
2. **World adapters**: the Kalshi client (request signing, fixed-point parsing, pagination, throttling), the snapshot builder and reader, evidence source adapters, and clocks.
3. **Knowledge**: the three retrieval indexes, as-of filtering, chunking, origin resolution, and ranking.
4. **State service**: the SQLite store and its MCP server.
5. **Model gateway and roles.**
6. **Forecast engine**: outcome spaces, the scenario tree, beam search, checks, the protected slot, and aggregation.
7. **Decision core**: fee model, sizing, risk gate, risk score, and routing. Pure functions.
8. **Execution and oversight**: paper executor, demo executor, review queue, halt, and stage manager.
9. **Evaluation**: backtest runner, metrics, ablations, report builder, and outcome resolver.
10. **Command line and configuration**: one entry point with a subcommand per run mode and operation, and one typed configuration holding every limit, threshold, source, model assignment, and followed series. Configuration is validated at startup, including that the caps are consistent with each other.

### Episode flow

1. **Trigger.** A new document from a followed source; a price move of 3¢ or more within 30 minutes on a followed event; or a schedule tick (fixed horizons in backtests, an optional daily re-check of open positions).
2. **Match.** Code retrieves the top five candidate events. The Monitor role reads their rules and confirms which event, if any, the trigger bears on. Exit: `no_match`.
3. **Research.** The Researcher role writes two queries, one for and one against. Code retrieves as of the episode time, merges duplicates, and returns the top eight passages. The role drops any that do not bear on the event, and code resolves origins. Exits: `no_evidence`, `insufficient_origins`. An episode started by a price move alone never trades without new evidence.
4. **Forecast.** Beam search over scenarios returns a probability per market, a confidence, and the supporting paths. Exits: `rules_mismatch` (every first-level scenario fails the rules check; one re-match is attempted first) and `low_coverage`.
5. **Size.** The decision core picks the one market in the event with the best fee-adjusted edge that meets the liquidity floor, computes a target position, and takes the difference from current holdings. Exit: `no_edge`.
6. **Gate.** Every limit is re-verified in code. Exit: `gate_rejected` with a reason. A rejected proposal is never retried at a larger size.
7. **Route.** The risk score assigns auto, approve, or halt. Approve pauses the graph with a review card until a human approves, resizes, or rejects. Exits: `rejected_by_human`, `expired`, `halted`.
8. **Execute.** The price is re-read. If it has moved past the edge-preserving limit, no order is sent: the episode recomputes once or exits `stale_price`. Otherwise an immediate-or-cancel limit order is placed and its fills are recorded. Exit: `executed`.
9. **Record.** Forecast, passage ids, decision, orders, cost, and latency go to the ledger. The outcome resolver later adds the result and the scores.

Each step is logged as thought (the role's short rationale), action (what code executed), and observation (what came back). Most episodes are expected to end in a no-trade exit.

### Knowledge (retrieval)

- **Events index.** One document per open event in the followed series: title, shared rules text, and strike list. Top five by cosine similarity, then the Monitor confirms. Full rules for the matched event are fetched by ticker.
- **Evidence index.** 250-word passages with a 50-word overlap. Headline and first paragraph stay together. Each passage is tagged with source, URL, publish time, and origin. Top eight after merging, at most three per origin. Ranking is similarity with a recency decay (half-life configurable per category, default 30 days). Near-duplicates and same-origin copies are merged. Passages below a similarity floor are dropped.
- **Ledger index.** One document per resolved forecast: question, probability, market price, outcome. Top five, same category first, resolved before the as-of time only.
- **As-of rule.** Every index filters on publish or resolution time inside the retrieval call. Callers cannot bypass it.
- **Origin rule.** A news-triggered trade needs one primary origin or two independent secondary origins. Confidence is capped by the independent-origin count.
- **Sources for v1.** Federal Reserve statements, minutes, and speeches; BLS CPI and Employment Situation releases; and a release calendar built from the FOMC and BLS schedules. Both agencies place their text in the public domain and ask to be credited.

### Forecast engine

- **Outcome space.** Built in code from the event's markets and their strike fields, through a small per-series registry. Categorical for a Fed meeting, whose event has five markets (hike more than 25 bp, hike 25, hold, cut 25, cut more than 25). A numeric ladder for CPI ("above 3.6%"). Every market maps to a subset of outcomes.
- **Tree.** The controller supplies the scheduled events between the as-of time and the close from the release calendar; the last level is always the resolution event itself. A thought is one claim about the next of those events, with the passage ids that support it. The generator proposes up to three per node. Depth is at most three.
- **Scoring.** Code checks run first and delete a scenario on failure: rules (it bears on the event as written), calendar (the claimed event happens before the close), and evidence (cited passages exist, predate the as-of time, and have counted origins). Then the critic scores each parent's set of sibling scenarios in one call. It sees the claims and their cited passages, never the generator's rationale. For each scenario it gives a probability given the parent and an outcome distribution if that path holds: a probability per option, or five quantiles for a ladder. Sibling probabilities sum to at most 1, and the remainder is unexplored mass. The critic may reject a scenario with a reason, which is stored so the generator does not repeat it, and may request more evidence once per scenario, which the controller routes to the Researcher.
- **Per-market probabilities.** Code converts outcome distributions into a probability for each market, so strikes on one ladder are always consistent. Quantiles are interpolated with a documented tail rule.
- **Search.** Beam search. At each depth the three scenarios with the highest running probability times evidence support are expanded, plus one protected slot. Evidence support is a 0-to-1 factor from the independent origins a scenario cites and their recency. Ties within 0.05 go to more independent origins, then to fresher evidence.
- **Frozen scenarios.** A scenario outside the beam is frozen, not discarded. It is not expanded further, and it stays in the forecast with its probability and its last outcome distribution. Discarding it would renormalize the forecast toward the likeliest path.
- **Protected slot.** For each scenario outside the top three, the controller re-aggregates the forecast without it and re-runs sizing. If its removal would flip the chosen trade's direction, change its size by more than one risk unit, or push a tradable market's probability to exactly 0 or 1, it is a candidate. The candidate that moves the size most is expanded too.
- **Aggregation.** A market's probability is the sum, over frozen scenarios and the deepest expanded ones, of path mass times that scenario's probability for the market, divided by their total mass. Coverage is that total mass. Below 0.6, too much of the outcome is unexplained and the episode exits `low_coverage`.
- **Confidence.** By default, coverage × min(1, independent origins ÷ 3), halved when the budget ran out. It can only reduce size.
- **Budget.** 30 model calls and 90 seconds per episode in live modes; the call budget alone in backtests. A full tree fits: at most nine generator and nine critic calls, plus matching, research, and a few evidence follow-ups. On exhaustion the scenarios scored so far are aggregated. An event closing within the hour gets depth 1 on the fast-path model.

### Decision core

Pure functions with no I/O and no model calls.

- **Numbers.** Prices are fixed-point dollars to four decimals and counts are fixed-point to two decimals, as the exchange sends them. All arithmetic uses decimals. v1 trades whole contracts and snaps limit prices to the market's valid ticks.
- **Fees.** The exchange's quadratic model: the taker fee is 0.07 × multiplier × contracts × price × (1 − price), the maker fee uses 0.0175, and both round up. Fee type and multiplier are read from the series, not hard-coded. Sizing rounds an order's total fee up to the next cent, and actual fees are reconciled from fills.
- **Shrinkage.** The forecast is pulled toward the market price by a per-category factor (default 0.5). Fitting the factor from the ledger is left for later.
- **Edge and size.** Edge is the shrunk probability minus the price paid minus the fee per contract, computed for buying YES and for buying NO. Minimum edge is 2¢. Size is quarter-Kelly on the fee-inclusive price, multiplied by confidence, floored to whole contracts, then clipped to the caps and to displayed depth.
- **Caps (defaults).** 5% of bankroll per market; 10% per event; 25 contracts per order; a 3% daily loss stop; a 10% drawdown halt; and a liquidity floor of $500 resting within 2¢ of the touch (a candle-volume proxy in backtests).
- **Gate.** Re-verifies, independently of sizing: halt flag, stage, blocklist, followed-series allowlist, origin rule, liquidity floor, every cap, limit-order-only, and a limit price inside the edge-preserving bound. It approves or rejects with a reason. It never modifies an order.
- **Risk score and routing.** Auto when cost is under one risk unit, the liquidity floor is met, the category is calibrated (at least 30 resolved forecasts with calibration error under a configured threshold), no protected scenario drove the size, and the price moved 3¢ or less during the episode. Otherwise approve. Halt on any stop condition. Auto is honored only at the autonomous stage. Earlier, would-be-auto trades still go to review and are logged as such.
- **Bankroll.** The demo subaccount's balance in demo mode; a configured notional amount in backtest and shadow.

### Execution and oversight

- **Paper executor.** In shadow, fills walk the live order book up to the limit price and pay taker fees; the unfilled remainder is dropped, as an immediate-or-cancel order would be. In backtests, fills use the first one-minute candle after the as-of time plus a configured latency, at the ask, capped at a configured share of that candle's volume. The exchange's history has no depth data, and the report says so. With no human present in backtests, approve-tier proposals are filled and tagged with their tier, so reports can separate them.
- **Demo executor.** Immediate-or-cancel limit orders through the exchange's current order endpoint, signed with a key that is restricted to one numbered subaccount and cannot transfer funds. Orders belong to an order group whose 50-contract limit over the exchange's rolling 15-second window cancels resting orders and blocks new ones when hit. Configuration validation keeps the per-order cap below the group's limit, so a single order never trips the breaker and a burst of orders does. Client order ids are deterministic, so a retry cannot double an order. Fills and fees are reconciled from the exchange.
- **Review.** Pending proposals persist. The review command shows the event and market, the forecast against the price, size and cost, the top three passages with dates, and the routing reasons. It accepts approve, resize, or reject with a reason code (stale evidence, wrong market, size too large, disagree with forecast, other). A proposal expires after ten minutes or when the price passes its edge bound. Approval re-runs the gate at the current price. The pause uses the graph's interrupt and checkpoint. The pausing node does nothing else, and order placement is a separate node, so resuming never repeats a side effect.
- **Halt.** One command sets a persisted flag that is checked before every order, and cancels all open orders. The agent halts itself on the daily loss stop, the drawdown limit, fill failures on more than one order in ten, a changed or withdrawn source behind an open position, or calibration error above threshold for two weeks.
- **Stages.** Backtest, shadow, approval, autonomous. Advancing requires the checkpoint 6.1 benchmarks, computed from the ledger: Brier skill above zero on 200 backtested markets; four shadow weeks with positive simulated P&L after fees and drawdown under 10%; and 100 approved trades with under 15% overridden. Two daily stops in one week demote a stage. The ladder governs real-money progression, which is out of scope here. The demo exchange may run at the approval stage regardless, because its funds are mock.
- **Blocklist.** A user-maintained list of series and tickers the agent must never trade, for markets where the user could hold non-public information.

### State service

One SQLite database holds: episodes and their ordered trace steps (role, input and output records, model, effort, tokens, cost, latency); tree nodes with scores, check results, and why each was expanded, frozen, or deleted; forecasts; proposals; reviews; orders and fills; ledger entries; the halt flag; and the stage.

An MCP server is its interface. It can write and read nodes and beams, record rejections, fetch rules and passages by id, search the three indexes with the as-of rule enforced server-side, query the ledger and calibration, and read any episode's trace. The controller uses it as the roles' shared state, and a reviewer can attach any MCP client to browse episodes. What a role may see is decided by which records the controller passes it. The critic's input never contains generator rationale.

### Model gateway and roles

- **Gateway.** Every model call goes through one function: role and input record in, validated output record out. It owns the map from role to model and effort; native JSON-schema structured output with one repair retry, then exit `error`; per-episode and per-run meters for calls, tokens, dollars, and seconds, with hard caps; a content-addressed response cache; cassette record and replay; and a scripted implementation for tests.
- **Record-only roles.** Roles emit records. Code executes every retrieval, check, and order. Evidence text is untrusted input passed in delimited blocks. Because no role can call a tool, an injected passage can at most distort a probability, and the caps bound what that can cost.
- **Schemas.** Output schemas ask for a rationale of one or two sentences, not step-by-step reasoning, which the API can refuse to extract.
- **Defaults.** Model ids live in configuration.

| Role | Model | Effort | Emits |
| --- | --- | --- | --- |
| Monitor | `claude-haiku-5-5` | low | Match decision |
| Researcher | `claude-haiku-5-5` | medium | Query plan; passage selection |
| Generator | `claude-haiku-5-5` | medium | Up to three scenario thoughts |
| Critic, depths 2–3 | `claude-sonnet-5-5` | high | Sibling probabilities, outcome distributions, rejections, evidence requests |
| Critic, depth 1 | `claude-fable-5-2` | high | Same |
| Fast path (event closes within the hour) | `claude-haiku-5-5` | low | Depth-1 generation and scoring |

- **CrewAI.** CrewAI defines the four roles and runs each as a single-agent kickoff bound to the gateway through a custom LLM adapter with tool calling off. CrewAI's documented Anthropic integration does not expose the effort setting, which is why effort and schema enforcement live in the gateway. If the adapter cannot carry them in the pinned version, the roles module calls the gateway directly with the same prompts, and nothing outside that module changes.

### Evaluation

- **Snapshot.** For the followed series (default `KXFEDDECISION` and `KXCPIYOY`; `KXFED` and `KXPAYROLLS` optional), every event closing between the window start and the build date: markets, rules, results, and one-minute and hourly candles. The builder reads both the live and the historical endpoints, because the exchange moves settled markets behind a cutoff that advances, and it throttles to the basic rate tier. The snapshot and the corpus are rebuilt by one command and are not committed.
- **Window.** Starts 2026-07-01, the day after the June 2026 knowledge cutoff listed for every default model. The run manifest records each model's listed cutoff, and a run refuses to start if any cutoff falls inside the window.
- **Schedules.** Fixed horizons before close (7, 3, and 1 days) for scoring. News-triggered replay for the trading simulation.
- **Systems compared.** The market price; prompt-only (one call to the strongest model in the pipeline, no retrieval); single-pass (the same call with retrieved passages); and the full pipeline. The ranking variants (similarity only against recency- and origin-aware) run on the single-pass system, where they are cheapest to compare.
- **Metrics.** Brier score; Brier skill against the market (1 − agent Brier ÷ market Brier); a reliability table; simulated P&L after fees against not trading; maximum drawdown; exits by reason; gate rejections by reason; cost, calls, and latency per episode; and the pruning audit (how often the path that happened ran through a scenario the search had frozen).
- **Statistics.** Bootstrap intervals clustered by event, with market and event counts reported side by side. The current window holds only a handful of settled events per series, each with several correlated markets (five per Fed meeting; one CPI event carried 21), so intervals will be wide and the report must say so.
- **Leakage controls.** The as-of rule inside the ports; a canary document dated after the as-of time that must never be retrieved; no web access in backtests; and cache keys that include the as-of time.
- **Cost controls.** A dry-run estimate, a dollar cap per run (default $25), the response cache, and scoring a subsample before a full run.
- **Artifacts committed for each reported run.** The manifest (configuration, model ids, effort levels, window, commit), a per-forecast table, metrics, a report with figures, and three to five full episode traces.

### Repository deliverables

- **README.** What the agent does and does not do; status; architecture diagram; the review-in-ten-minutes path; a quickstart for offline replay; setup with keys; run modes; configuration; guardrails and where each is enforced; a results summary linked to the report; limitations; an educational-use and not-financial-advice statement; license; and an acknowledgment of the CMU program.
- **Docs.** Architecture; guardrails and human oversight; evaluation method and results; short decision records citing the checkpoint each came from; and the glossary.
- **Examples.** Sample inputs (trigger, event snapshot, passages), sample outputs (trace, forecast, decision, review card), and the cassettes that replay them.
- **Notebook.** Loads the committed evaluation artifacts and reproduces the report's figures.
- **Hygiene.** MIT license; an environment template; secrets read only from the environment and never logged; the database, snapshot, corpus, and caches ignored by git; committed text limited to public-domain sources and the project's own outputs; and exchange data samples kept small until the exchange's data terms are checked.

## Testing Decisions

A good test here asserts what an episode decided, given a fixed world: its exit, forecast, proposal, routing tier, and orders. It does not assert prompt wording, call order, or which framework ran a role. Model quality is measured by the evaluation harness, not by unit tests.

**Seam 1: the episode boundary.** Tests run whole episodes against a recorded world with scripted model records and assert the episode record. Scenarios, named after the checkpoint cases they come from:

1. No matching event → `no_match`.
2. One secondary origin, syndicated twice → `insufficient_origins`.
3. The market already prices the news (the 2.1 phantom edge) → `no_edge`.
4. Hawkish Fed minutes (the 3.1 example) → a small NO position.
5. Every first-level scenario fails the rules check → one re-match, then `rules_mismatch`.
6. A low-probability scenario whose removal flips the trade takes the protected slot and is expanded (the 5.1 rule). One that changes nothing is frozen. Both still count in the forecast.
7. A proposal over a cap → `gate_rejected`, with no retry.
8. The price moves past the edge before execution → no order is sent.
9. Approve tier: after the pause, approve, resize, and reject each produce the right record, and an expired proposal never executes.
10. Halt flag set → no order in any mode.
11. Call budget exhausted → a forecast from the current beam with reduced confidence.
12. Event closing within the hour → the depth-1 path.
13. A canary document dated after the as-of time is never retrieved.
14. Partial fill → the filled part is recorded and the remainder is gone.
15. Malformed model output → one repair retry, then exit `error` with no order.
16. Ladder event → per-market probabilities are monotone across strikes.

**Seam 2: the decision core.** Property-based tests on the pure functions:

- Size never exceeds any cap, for any input.
- Size is zero whenever edge after fees is below the minimum.
- Size does not grow when confidence falls.
- Direction always matches the sign of the edge.
- The fee function reproduces worked examples from the exchange's schedule.
- The gate never returns a modified order.
- A tripped loss stop stays tripped for the day.

**Adapter contracts.** Each adapter is tested at its port against recorded responses: request signing against a known vector, fixed-point parsing, pagination, the live-plus-historical merge, and order payloads. An opt-in smoke test exercises the demo exchange and one model call when keys are present. It never runs in CI.

**Evaluation math.** Brier, skill, the clustered bootstrap (seeded), and P&L accounting are checked on small hand-computed cases.

**Prior art.** None; the repository is new. The pattern is record and replay, with fixtures small enough to read.

**CI.** Lint and the full offline suite on every push, with no secrets, in under five minutes.

## Out of Scope

- Real-money trading, and any connection that can place orders on the production exchange.
- Resting or market-making orders, websockets, and latency-sensitive strategies.
- Series beyond the followed economics series; sports, combos, and perpetuals.
- News wires, social media, and open-web search as evidence sources. The evidence port allows them later.
- Venues other than Kalshi.
- A hosted deployment, a scheduler service, or a live web dashboard. Monitoring is a generated report.
- Fractional contracts and sub-cent pricing strategies.
- Model fine-tuning, automated prompt optimization, and LLM-written calibration reviews.
- Fitting shrinkage factors or thresholds from the ledger. v1 uses configured defaults.
- Multi-user support, tax reporting, and accounting.

## Further Notes

### Changes from the checkpoint documents

The final report can describe these as the design evolving during the build.

1. **Forecasts are per event, not per market.** Real events come as ladders (one CPI event had 21 strike markets). One tree per event yields consistent probabilities for all of them and costs one search, not 21.
2. **The events index holds one document per event**, not one per market, for the same reason.
3. **Roles are record-only.** The checkpoints had agents calling tools. Here code executes every tool call, and the critic's evidence request is a field in its output. This closes the prompt-injection path from evidence text to actions.
4. **The origin rule gains a primary-source case.** An official release is the ground truth, so it does not need a second source. Secondary sources still need two.
5. **The 4.1 probability floor is gone.** The 5.1 decision-aware protected slot is the only tail rule, extended from one market to an event.
6. **Scenarios outside the beam are frozen, not discarded.** They stop being expanded and still count in the forecast. The protected slot now decides which extra scenario gets a deeper look.
7. **The critic scores sibling scenarios together.** This keeps their probabilities coherent and fits the 30-call budget.
8. **The depth-1 critic defaults to Fable 5.2**, the current listing and cheaper than the Fable 5.1 named in checkpoint 4.1. One configuration value reverts it.
9. **Dashboards are generated reports**, not a live service.
10. **Real-money trading is out of scope.** The autonomy ladder is kept as stored state and benchmarks.
11. **Evidence is official sources only in v1.** News wires and open-web search are deferred, which also keeps committed text in the public domain.
12. **Orders are immediate-or-cancel limit orders.** There are no resting orders in v1.

Defaults introduced here, all configurable: the 3¢-in-30-minutes price trigger, the 30-day recency half-life, the 2¢ minimum edge, the 0.5 shrinkage factor, the ten-minute proposal expiry, the 25-contract order cap, the confidence formula, the notional bankroll, and the $25 run cap.

### Build order

Each step leaves something runnable. Steps 1–9 are the minimum reviewable project.

1. Scaffold: packaging, configuration, command-line skeleton, CI, README skeleton, license, environment template.
2. Decision core with its property tests.
3. Walking skeleton: one episode end to end on a hand-made fixture with scripted models and the paper executor; the offline replay command; the first committed sample episode.
4. Kalshi read client, snapshot builder, and events index.
5. Evidence sources, chunking, as-of retrieval, and origin logic.
6. Model gateway, roles, and cassettes; the first real episode on a categorical event.
7. Forecast engine (beam search, checks, protected slot) and the state service with its MCP server.
8. Backtest runner, metrics, and report; the first committed evaluation with the prompt-only baseline.
9. Shadow mode on live data, the review queue and commands, and halt.
10. Demo executor on the Kalshi demo exchange.
11. Numeric ladder events and the CPI series. This comes first among the extras because it multiplies the evaluation sample.
12. The remaining ablations and the ranking variants.
13. Stage manager commands, the notebook, and a generic feed source.

The next Fed meeting is December 8–9, 2026. A shadow-mode run across it would give the recorded presentation a live episode with a known resolution date.

### Facts checked on 2026-11-24

Re-check these when pinning versions. Several differ from what older examples and model training data show.

- **Kalshi environments.** Production REST is `https://external-api.kalshi.com/trade-api/v2` and demo is `https://external-api.demo.kalshi.co/trade-api/v2`. Credentials are separate per environment. Demo uses mock funds.
- **Kalshi authentication.** Headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (milliseconds), and `KALSHI-ACCESS-SIGNATURE` over timestamp + method + path without the query string. Keys are Ed25519 or RSA (RSA-PSS with SHA-256).
- **Kalshi public reads.** The market-data quick start says series, markets, events, and order books need no authentication headers. The series, markets, and historical endpoints answered unauthenticated on the check date.
- **Kalshi orders.** `POST /portfolio/events/orders` with `side` (`bid` or `ask`, on the YES leg), `price` and `count` as fixed-point strings, `time_in_force` (`good_till_canceled`, `immediate_or_cancel`, `fill_or_kill`), and `self_trade_prevention_type`. The older order-write endpoints return 410.
- **Kalshi fixed-point.** Prices are dollar strings with up to four decimals, counts are strings with two, and each market's valid ticks come from its `price_ranges`.
- **Kalshi history.** `GET /historical/cutoff` gives the cutoffs. Markets settled before `market_settled_ts` (2026-09-24 on the check date) are served by `/historical/markets` and `/historical/markets/{ticker}/candlesticks`; newer ones by the live endpoints. Candle intervals are 1, 60, and 1440 minutes with bid and ask OHLC, volume, and open interest.
- **Kalshi events.** A `KXFEDDECISION` event has five markets: hike more than 25 bp, hike 25, hold, cut 25, and cut more than 25. The September 2026 `KXCPIYOY` event had 21 threshold markets.
- **Kalshi fees.** The schedule effective 2026-07-07 gives taker `round up(M × 0.07 × C × P × (1−P))` and maker `round up(M × 0.0175 × C × P × (1−P))`. Series expose `fee_type` and `fee_multiplier`. The four followed series are `quadratic_with_maker_fees` with multiplier 1.
- **Kalshi limits.** The basic tier allows 200 read and 100 write tokens per second at 10 tokens per default request, and returns 429 beyond that.
- **Kalshi order groups.** A contracts limit over a rolling 15-second window. Hitting it cancels the group's resting orders and rejects new ones until reset.
- **Kalshi subaccounts.** Numbered 1–63. A key can be restricted to one, and a restricted key cannot transfer funds or manage subaccounts or keys. Creating one needs the Advanced tier in production, which demo does not enforce.
- **Claude models.** `claude-fable-5-2` ($8 in, $40 out per million tokens), `claude-opus-5-5` ($4, $20), `claude-sonnet-5-5` ($2, $10), `claude-haiku-5-5` (from $0.10, $0.50). All four list a June 2026 reliable knowledge cutoff. Fable 5.1 is still listed at $10 and $50.
- **Claude API.** Effort is `output_config.effort` (low, medium, high, xhigh, max). Structured output is `output_config.format` with a JSON schema.
- **LangChain.** `ChatAnthropic` accepts `effort`, and `with_structured_output(..., method="json_schema")` uses native structured output.
- **CrewAI.** Python 3.10 to 3.13. Native Anthropic support through the `anthropic` extra. `Agent.kickoff(..., response_format=...)` returns a parsed model. Custom LLMs subclass `BaseLLM`. The Anthropic section does not document effort.
- **MCP Python SDK.** Package `mcp`; v2 is the stable line with `MCPServer` as its server class, and v1 users must pin below 2. Check that any adapter package supports v2 before pinning.
- **LangGraph.** `interrupt()` needs a checkpointer and a thread id, resumes with `Command(resume=...)`, and re-runs the interrupted node from its start.
- **Sources.** Federal Reserve and BLS site content is public domain unless marked otherwise, and both ask for credit. The 2026 Fed meetings of July 28–29, September 15–16, and October 27–28 have happened.

Reference pages:

- Kalshi documentation index: https://docs.kalshi.com/llms.txt
- Kalshi fee schedule: https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Claude models: https://platform.claude.com/docs/en/models/overview
- Claude effort: https://platform.claude.com/docs/en/build-with-claude/effort
- Claude structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- LangChain Anthropic integration: https://docs.langchain.com/oss/python/integrations/chat/anthropic
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- CrewAI agents, LLMs, custom LLMs, and MCP: https://docs.crewai.com/en/concepts/agents, https://docs.crewai.com/en/concepts/llms, https://docs.crewai.com/en/learn/custom-llm, https://docs.crewai.com/en/mcp/overview
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Federal Reserve meeting calendar and reuse notice: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm, https://www.federalreserve.gov/disclaimer.htm
- BLS reuse notice: https://www.bls.gov/bls/linksite.htm

### Risks

- **Framework drift.** The MCP SDK has moved to a v2 line, and the CrewAI and LangChain integrations change quickly. The gateway and the state service isolate them, and the CrewAI path has a stated fallback.
- **Small sample.** A few months of post-cutoff events cannot separate skill from luck. The harness and its reporting are the deliverable; the numbers are a pilot.
- **Cost.** Thinking counts as output, so high-effort calls cost more than their visible text suggests. The first runs should measure cost per episode before any full backtest.
- **Source access.** Official sites can throttle or refuse automated clients. Fetch politely, cache everything, follow each site's terms, and fall back to downloading the handful of releases by hand.
- **Demo differences.** Events on the demo exchange may not match production. The demo executor should pick whatever followed-series event exists there.
- **Time.** Steps 10–13 are extras. Cutting them leaves a complete project.

### Publishing

No repository or issue tracker existed when this was written, so the spec is delivered as a file. Once the repository exists: run the skills' setup with GitHub Issues as the tracker, publish this file as the first issue with the `ready-for-agent` label, and split the build order into tickets. A descriptive working name is `prediction-market-forecasting-agent`.
