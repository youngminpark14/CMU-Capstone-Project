# Glossary

One vocabulary for the code, the docs and the traces. Each entry says where the term lives. Terms
from the specification come first, then the terms the code adds.

## Terms from the specification

**Series, event, market.** The exchange's terms. A series (monthly CPI, `KXCPIYOY`) has events
(September CPI); an event has one market per option or strike, each a YES/NO contract.
`Series`, `Event`, `Market` in [records/market.py](../src/pmfa/records/market.py).

**Outcome space.** The mutually exclusive, exhaustive outcomes of an event, built in code from its
markets: options for a categorical event (a Fed decision), numeric bins between strikes for a
ladder (CPI). Every market maps to the outcomes on which it resolves YES. `OutcomeSpace` in
[records/forecast.py](../src/pmfa/records/forecast.py), built in
[forecast/outcomes.py](../src/pmfa/forecast/outcomes.py).

**Trigger.** What starts an episode: a new document, a price move, or a schedule tick. Its time is
the episode's as-of time; a backtest trading episode that has to wait for an earlier order's
outcome runs on a copy re-dated to its start (see *Late start*). A price-move or schedule trigger names its event. `Trigger` in
[records/evidence.py](../src/pmfa/records/evidence.py).

**Episode.** One pass of the pipeline for one trigger and one event. Its id is deterministic, so
running the same trigger twice returns the first result. `EpisodeRecord` in
[records/episode.py](../src/pmfa/records/episode.py); run by
[episode/controller.py](../src/pmfa/episode/controller.py).

**Exit.** The one named way an episode ended: `no_match`, `no_evidence`, `insufficient_origins`,
`rules_mismatch`, `low_coverage`, `no_edge`, `gate_rejected`, `rejected_by_human`, `expired`,
`halted`, `stale_price`, `executed`, or `error`. `Exit` in [records/episode.py](../src/pmfa/records/episode.py).

**As-of time.** The moment an episode runs "at". Nothing published or priced after it may be read;
every retrieval and exchange read filters on it inside the call. The one exception is the candle
that prices a backtest fill, which no decision reads and which counts only from the order's
*outcome time*. `EpisodeRecord.as_of`.

**Passage.** A chunk of about 250 words of an evidence document (50 words of overlap), tagged with
its source, URL, publish time and origin. Its id is `{doc_id}#{n}`. `Passage` in
[records/evidence.py](../src/pmfa/records/evidence.py); made by
[knowledge/chunking.py](../src/pmfa/knowledge/chunking.py).

**Origin.** A publisher's original document after its copies are merged. Two documents are one
origin when one is substantially a copy of the other's text: their normalised texts are identical,
or at least 92% of the smaller body's five-word shingles appear in the larger and the smaller has
at least half as many shingles. Shared topic or template wording is not enough. Copies count as one origin, so repetition does not
look like corroboration. `Origin`; resolved in
[knowledge/origins.py](../src/pmfa/knowledge/origins.py).

**Primary origin.** An origin from an allowlisted official publisher for the event's category
(`federalreserve.gov` for Fed events, `bls.gov` for CPI and payrolls). One primary origin, or two
independent secondary ones, meet the origin rule. `Origin.is_primary`; the list is
`primary_sources` in [config.py](../src/pmfa/config.py).

**Scenario.** A path of assumed outcomes for the scheduled events before resolution, with its
probability given its parent, its path mass, and an outcome distribution if the path holds. Each
scenario is one node of the tree. `TreeNode` in [records/forecast.py](../src/pmfa/records/forecast.py).

**Beam.** The scenarios at one depth that are expanded at the next: the 3 with the highest path
mass times evidence support. [forecast/support.py](../src/pmfa/forecast/support.py),
[forecast/engine.py](../src/pmfa/forecast/engine.py).

**Protected slot.** One extra expansion per level, for the scenario outside the beam whose removal
would flip the trade, change its cost by more than one risk unit, or push a tradable market to
exactly 0 or 1. `Forecast.protected_node_id`; chosen in [forecast/engine.py](../src/pmfa/forecast/engine.py).

**Frozen.** The status of a scored scenario outside the beam and the protected slot. It is not
expanded further and still counts in the forecast with its probability and last outcome
distribution. `NodeStatus.FROZEN`.

**Forecast.** A probability for every market of the event, with coverage, confidence, the number
of independent origins, and the supporting paths. `Forecast` in
[records/forecast.py](../src/pmfa/records/forecast.py).

**Edge.** The forecast probability of a side, after shrinkage toward the market price, minus the
price paid, minus the fee per contract. Sizing needs at least 2¢. `Proposal.edge`; computed in
[decision/sizing.py](../src/pmfa/decision/sizing.py).

**Proposal.** A sized order intent: buy this many contracts of one side of one market at no more
than a limit price. It carries the edge, the fee, the worst-case cost and an expiry time.
`Proposal` in [records/decision.py](../src/pmfa/records/decision.py).

**Gate.** The code that re-verifies every limit on a proposal and approves or rejects it, naming
every failed check. It never changes the order. `gate()` in
[decision/gate.py](../src/pmfa/decision/gate.py); `GateResult` in
[records/decision.py](../src/pmfa/records/decision.py).

**Routing tier.** Who decides on an approved proposal: `auto` (code places it), `approve` (a human
decides), or `halt` (a stop condition holds). `Routing` and `RoutingTier`; assigned in
[decision/routing.py](../src/pmfa/decision/routing.py).

**Ledger.** The durable record of forecasts: one entry per market per episode, with the
probability, the price at forecast time, and later the outcome, the Brier scores, the tier and any
override. `LedgerEntry` in [records/episode.py](../src/pmfa/records/episode.py); stored by
[state/store.py](../src/pmfa/state/store.py).

**Stage.** The autonomy level: backtest, shadow, approval, autonomous. It advances only when the
next stage's benchmark holds (scored forecasts, shadow fills, or human reviews). `Stage` in [records/base.py](../src/pmfa/records/base.py);
managed by [execution/stages.py](../src/pmfa/execution/stages.py).

**World.** The set of ports an episode sees: clock, exchange, executor, evidence, knowledge,
models, state store, and the review policy. `World` in [ports.py](../src/pmfa/ports.py).

**Run mode.** A choice of world: replay, backtest, shadow or demo. `RunMode` in
[records/base.py](../src/pmfa/records/base.py).

**Cassette.** A recorded list of an episode's model calls, in order, with each input and output
record, replayed offline with no key. A replayed call must ask for the same task with the same
input, or replay stops. [models/cassette.py](../src/pmfa/models/cassette.py).

**Risk unit.** 1% of bankroll. Auto routing needs a cost under one risk unit, and the protected slot
counts a size change of more than one risk unit as decision-changing. `caps.risk_unit_fraction` in
[config.py](../src/pmfa/config.py).

## Terms the code adds

**Level.** One depth of the scenario tree: one scheduled event the Generator makes claims about.
Up to two calendar entries before the close come first; the event's resolution is always last.
`ScheduledEventBrief`; chosen in [forecast/levels.py](../src/pmfa/forecast/levels.py).

**Path mass.** A scenario's running probability: the product of the Critic's conditional
probabilities down its path. `TreeNode.path_mass`.

**Evidence support.** A factor in [0, 1] for how well a scenario's own citations back it: the sum,
over the independent origins it cites, of 2 (primary) or 1 (secondary) times a recency decay with
a 30-day half-life, divided by 2 and capped at 1. `TreeNode.evidence_support`; computed in
[forecast/support.py](../src/pmfa/forecast/support.py).

**Coverage.** The total path mass of the scenarios that make up the forecast. The mass the Critic
left unassigned among siblings lowers it. Below 0.6 the episode exits `low_coverage`.
`Forecast.coverage`; [forecast/aggregate.py](../src/pmfa/forecast/aggregate.py).

**Confidence.** Coverage times min(1, independent origins cited by the forecast's scenarios / 3),
halved when an episode cap (calls, dollars or seconds) ended the search. It multiplies the Kelly
size, so it can only shrink an order.
`Forecast.confidence`; [forecast/aggregate.py](../src/pmfa/forecast/aggregate.py).

**Fast path.** The search for an event closing within 60 minutes: the resolution level alone,
generated and scored on the fast-path model slot. `Forecast.fast_path`;
[forecast/levels.py](../src/pmfa/forecast/levels.py).

**Fill-tagged.** An approve-tier proposal filled with no human present, as in a backtest. The
episode record says so, so reports can separate these fills from human-approved ones.
`EpisodeRecord.fill_tagged`; `ReviewPolicy.FILL_TAGGED` in
[records/decision.py](../src/pmfa/records/decision.py).

**Would-be-auto.** A proposal that met every auto condition but was sent to review because the stage
is below autonomous. The flag shows on the review card and in the trace and is counted in the
reports; the approval benchmark counts every human review alike, flagged or not.
`Routing.would_be_auto`.

**Execution book.** The order book the pipeline decides with when it is about to send: the price
re-read before sending, the recompute when nothing is offered at the limit, the gate after a
review, and routing's price move. It is the live book in shadow and demo mode, the recorded
execution-time book in replay, and in a backtest the as-of book, so no decision reads a price from
after the as-of time. A backtest order then fills against the *fill book*, which is priced later
and counts only from its *outcome time*. `Executor.execution_book` in
[ports.py](../src/pmfa/ports.py).

**Fill book.** The book an order actually fills against. In shadow and demo mode it is the
execution book. In a backtest it is a book built from the one-minute candle that holds the order's
arrival time (the as-of time plus the latency, 60 seconds), on the conservative side of that
candle, else the hourly candle, else an empty book; no decision sees it. In replay it is the
recorded fill book when the recording has one, else the execution book. `fill_books` on the paper
executor ([execution/paper.py](../src/pmfa/execution/paper.py));
[world/snapshot/book.py](../src/pmfa/world/snapshot/book.py).

**Outcome time.** When an order's outcome (its fills, or that it did not fill) is known. In a
backtest it is the end of the candle that priced its fill book; an hour after the arrival time (the
end of the hour searched) when no candle was found; the arrival time when the market had closed by
then. The paper executor stamps the fills with it. The paper account, the equity points and the
stops that read orders (fill failures, a changed source) count an order only from then, so no
decision made earlier sees its price or outcome. `OrderResult.outcome_at`, set only when later than
`placed_at`, in [records/decision.py](../src/pmfa/records/decision.py).

**Late start.** A backtest trading episode that starts later than its trigger. Each episode of the
trading pass starts at the latest of its trigger's time, the clock's time and the latest stored
outcome time, so it waits for the outcome of every earlier order, and the time it starts at is its
as-of time. `metrics.json` counts them under `late_starts`. `_trading_pass` in
[evaluation/backtest.py](../src/pmfa/evaluation/backtest.py).

**Marked equity.** Cash plus each open holding at the current best bid on its own side (what it
could be sold for now), or at cost when that side has no bid. The equity points behind the daily
loss stop and the drawdown halt are marked, so both stops count unrealized losses. The bankroll
that sizing and the dollar caps use is cash plus holdings at cost. `marked_equity` in
[execution/portfolio.py](../src/pmfa/execution/portfolio.py).

**Canary.** A document dated after every forecast, placed in the backtest's evidence index. Retrieving
it means the as-of rule failed, and the run stops. `evaluation.canary_text` in
[config.py](../src/pmfa/config.py); [world/backtest.py](../src/pmfa/world/backtest.py).

**Content digest.** A SHA-256 over a snapshot's data rows alone (series, events, markets, candles
and coverage, in a fixed order), so two builds that hold the same rows give the same digest while
their file hashes differ. The snapshot manifest stores it as `content_sha256` and a run manifest as
`snapshot_content_sha256`. `content_sha256` in
[world/snapshot/store.py](../src/pmfa/world/snapshot/store.py).

**Rehearsal.** A whole backtest on the frozen data with a fixed rule in place of every model answer,
run before any paid run. Its forecasts are fixed rules and mean nothing. It checks the data and the
pipeline around the models: the run completes, no pipeline episode ends because an answer was
refused or a step failed, the pipeline forecasts and sends at least one order, and at least one
trace is saved and every saved trace replays and matches. It does not reach the model side (the
gateway's schemas, the cache, the dollar caps), and its critic never rejects. Its manifest says its
models were supplied by the caller, so `pmfa report --publish` refuses it.
[scripts/rehearse_backtest.py](../scripts/rehearse_backtest.py).

**Abstention.** An episode that ended without a forecast (an exit before or during the forecast
step). In the evaluation its row has no probability; the stricter skill figure scores it as the
market price. `ForecastRow.probability` in [evaluation/rows.py](../src/pmfa/evaluation/rows.py).

**Model slot.** A named model, effort and token limit in the configuration: `monitor`, `researcher`,
`generator`, `critic_depth1`, `critic_deep`, `fast_path`, `baseline`. `ModelSlot` in
[records/roles.py](../src/pmfa/records/roles.py).

**Shrinkage.** Pulling the forecast toward the market price before computing edge:
`0.5 x mid + 0.5 x forecast` by default. `SeriesConfig.shrinkage`; `shrink()` in
[decision/sizing.py](../src/pmfa/decision/sizing.py).

**Edge bound.** The highest valid price on a side that still leaves the minimum edge after fees. The
limit price never exceeds it, and a proposal expires once the best ask passes it.
`Proposal.edge_bound_price`.

**Trace.** An episode's ordered steps, each a *thought* (a role's short rationale), an *action*
(what code executed) or an *observation* (what came back). `TraceStep` in
[records/episode.py](../src/pmfa/records/episode.py); written by
[episode/trace.py](../src/pmfa/episode/trace.py).

**Pruning audit.** After an event resolves, the share of the posterior (path mass times each
scenario's probability of what happened) that sat on frozen scenarios.
[forecast/audit.py](../src/pmfa/forecast/audit.py).
