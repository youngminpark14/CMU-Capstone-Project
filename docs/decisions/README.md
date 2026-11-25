# Decision records

Each record says what was decided, why, what follows from it, and where it came from: a capstone checkpoint the specification names, a section of [spec.md](../spec.md), or the build itself. Records 0001 to 0012 are the specification's "Changes from the checkpoint documents". The checkpoint documents themselves are not available to this project, so a record names a checkpoint only where the specification does (0005, 0008, 0010) and otherwise says it is not named. Records 0013 onward were made while building and checked against the code.

| # | Decision | Origin |
| --- | --- | --- |
| [0001](0001-forecast-per-event.md) | Forecast per event, not per market | spec; checkpoint not named |
| [0002](0002-events-index-per-event.md) | The events index holds one document per event, one row per listing change | spec; checkpoint not named |
| [0003](0003-record-only-roles.md) | Roles are record-only | spec; checkpoint not named |
| [0004](0004-origin-rule-primary-case.md) | The origin rule gains a primary-source case | spec; checkpoint not named |
| [0005](0005-protected-slot-only-tail-rule.md) | The protected slot is the only tail rule | checkpoints 4.1 and 5.1 |
| [0006](0006-frozen-not-discarded.md) | Scenarios outside the beam are frozen, not discarded | spec; checkpoint not named |
| [0007](0007-critic-scores-siblings.md) | The Critic scores sibling scenarios together | spec; checkpoint not named |
| [0008](0008-depth1-critic-fable-5-2.md) | The depth-1 Critic defaults to Fable 5.2 | checkpoint 4.1 |
| [0009](0009-generated-reports.md) | Dashboards are generated reports | spec; checkpoint not named |
| [0010](0010-no-real-money.md) | Real-money trading is out of scope | spec; checkpoint 6.1 |
| [0011](0011-official-sources-only.md) | Evidence is official sources only in v1 | spec; checkpoint not named |
| [0012](0012-ioc-limit-orders.md) | Orders are immediate-or-cancel limit orders | spec; checkpoint not named |
| [0013](0013-two-dependency-sets.md) | Two dependency sets: CrewAI with MCP SDK 1.x, or MCP SDK 2.x without CrewAI | build, November 2026 |
| [0014](0014-crewai-single-agent-kickoffs.md) | Roles run as single-agent CrewAI kickoffs bound to the gateway | build, November 2026 |
| [0015](0015-claude-api-usage.md) | How the Claude API is called | build, November 2026 |
| [0016](0016-what-a-role-sees.md) | What a role may see | build, November 2026 |
| [0017](0017-named-event-triggers-skip-monitor.md) | Triggers that name their event skip the Monitor | build, November 2026 |
| [0018](0018-selling-is-buying-the-other-side.md) | Selling is buying the other side | build, November 2026 |
| [0019](0019-fees-in-sizing.md) | How fees enter sizing | build, November 2026 |
| [0020](0020-gate-independent-of-sizing.md) | The gate recomputes every bound and never returns an order | build, November 2026 |
| [0021](0021-rules-check-scope.md) | What the rules check can and cannot verify | build, November 2026 |
| [0022](0022-support-ties-confidence.md) | Evidence support, the tie rule, and confidence | build, November 2026 |
| [0023](0023-ladder-distributions.md) | Ladder distributions from five quantiles | build, November 2026 |
| [0024](0024-tree-levels.md) | Tree levels: upcoming scheduled events, then the resolution | build, November 2026 |
| [0025](0025-pruning-audit.md) | The pruning audit as posterior mass on frozen scenarios | build, November 2026 |
| [0026](0026-retrieval-embedder-and-as-of.md) | Retrieval: a hashing embedder by default, and as-of rounding that excludes | build, November 2026 |
| [0027](0027-record-and-replay.md) | Record and replay with ordered cassettes | build, November 2026 |
| [0028](0028-backtest-data-and-fills.md) | Backtest quotes, fills and liquidity from candles; decisions read the as-of book | build, November 2026 |
| [0029](0029-source-access.md) | Source access: manual BLS import, hand-computed fee cases | build, November 2026 |
| [0030](0030-scoring-choices.md) | Scoring choices: abstentions as the market, intervals by event, the market price | build, November 2026 |
| [0031](0031-state-transport.md) | Shared state goes through the MCP server in process, and directly in backtests | build, November 2026 |
| [0032](0032-demo-effective-stage.md) | Demo mode always runs at the approval stage | build, November 2026 |
| [0033](0033-build-defaults.md) | Defaults introduced while building | build, November 2026 |
| [0034](0034-continued-episodes-reconcile.md) | A continued episode reconciles and never places an order | build, November 2026 |
| [0035](0035-marked-equity-for-the-stops.md) | The stops read marked equity; sizing's bankroll stays at cost | build, November 2026 |
| [0036](0036-dollar-caps-are-ceilings.md) | The dollar caps are ceilings, checked against each call's worst case | build, November 2026 |
| [0037](0037-price-move-triggers-anchored.md) | Price-move triggers are anchored to the event's last trigger | build, November 2026 |
| [0038](0038-origins-by-text-overlap.md) | Origins are merged by text overlap, not vector similarity | build, November 2026 |
