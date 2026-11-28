# Decision records

Each record says what was decided, why, what follows from it, and where it came from. Records 0001 to 0012 are the [specification](../spec.md)'s "Changes from the checkpoint documents"; each names the checkpoints it changes, says what they proposed, and names the instructors' feedback where the change answers it. Records 0013 onward were made while building and checked against the code; where one implements, changes or answers an item of a checkpoint or its feedback, its Origin says which in one sentence. The checkpoint documents (1.1 to 6.1, each with the instructors' feedback) are the project owner's coursework: they were read on 2026-11-27 and are not committed here. In the Origin column, "checkpoints 3.1 and 4.1; spec item 1" names the checkpoints a specification change revises, "build; checkpoint 5.1" names the checkpoints a build decision's Origin refers to, and "build" means no checkpoint is behind it.

Read 0001 to 0012 first (how the design changed from the checkpoints), then 0013, 0016, 0020, 0028, 0030, 0031, 0032 and 0034; the rest record build details and review fixes.

| # | Decision | Origin |
| --- | --- | --- |
| [0001](0001-forecast-per-event.md) | Forecast per event, not per market | checkpoints 3.1 and 4.1; spec item 1 |
| [0002](0002-events-index-per-event.md) | The events index holds one document per event, one row per listing change | checkpoint 3.1; spec item 2 |
| [0003](0003-record-only-roles.md) | Roles are record-only | checkpoints 2.1, 4.1 and 5.1; spec item 3 |
| [0004](0004-origin-rule-primary-case.md) | The origin rule gains a primary-source case | checkpoints 2.1, 3.1, 5.1 and 6.1; spec item 4 |
| [0005](0005-protected-slot-only-tail-rule.md) | The protected slot is the only tail rule | checkpoints 4.1 and 5.1; spec item 5 |
| [0006](0006-frozen-not-discarded.md) | Scenarios outside the beam are frozen, not discarded | checkpoint 4.1; spec item 6 |
| [0007](0007-critic-scores-siblings.md) | The Critic scores sibling scenarios together | checkpoint 4.1; spec item 7 |
| [0008](0008-depth1-critic-fable-5-2.md) | The depth-1 Critic defaults to Fable 5.2 | checkpoint 4.1; spec item 8 |
| [0009](0009-generated-reports.md) | Dashboards are generated reports | checkpoint 6.1; spec item 9 |
| [0010](0010-no-real-money.md) | Real-money trading is out of scope | checkpoints 1.1, 2.1 and 6.1; spec item 10 |
| [0011](0011-official-sources-only.md) | Evidence is official sources only in v1 | checkpoints 1.1, 2.1 and 3.1; spec item 11 |
| [0012](0012-ioc-limit-orders.md) | Orders are immediate-or-cancel limit orders | checkpoints 2.1 and 6.1; spec item 12 |
| [0013](0013-two-dependency-sets.md) | Two dependency sets: CrewAI with MCP SDK 1.x, or MCP SDK 2.x without CrewAI | build |
| [0014](0014-crewai-single-agent-kickoffs.md) | Roles run as single-agent CrewAI kickoffs bound to the gateway | build; checkpoint 4.1 |
| [0015](0015-claude-api-usage.md) | How the Claude API is called | build; checkpoints 4.1 and 5.1 |
| [0016](0016-what-a-role-sees.md) | What a role may see | build; checkpoints 2.1 and 5.1 |
| [0017](0017-named-event-triggers-skip-monitor.md) | Triggers that name their event skip the Monitor | build; checkpoint 5.1 |
| [0018](0018-selling-is-buying-the-other-side.md) | Selling is buying the other side | build; checkpoint 1.1 |
| [0019](0019-fees-in-sizing.md) | How fees enter sizing | build; checkpoint 2.1 |
| [0020](0020-gate-independent-of-sizing.md) | The gate recomputes every bound and never returns an order | build; checkpoints 2.1, 5.1 and 6.1 |
| [0021](0021-rules-check-scope.md) | What the rules check can and cannot verify | build; checkpoint 4.1 |
| [0022](0022-support-ties-confidence.md) | Evidence support, the tie rule, and confidence | build; checkpoints 3.1 and 4.1 |
| [0023](0023-ladder-distributions.md) | Ladder distributions from five quantiles | build |
| [0024](0024-tree-levels.md) | Tree levels: upcoming scheduled events, then the resolution | build; checkpoint 4.1 |
| [0025](0025-pruning-audit.md) | The pruning audit as posterior mass on frozen scenarios | build; checkpoint 4.1 |
| [0026](0026-retrieval-embedder-and-as-of.md) | Retrieval: a hashing embedder by default, and as-of rounding that excludes | build; checkpoint 3.1 |
| [0027](0027-record-and-replay.md) | Record and replay with ordered cassettes | build; checkpoint 6.1 |
| [0028](0028-backtest-data-and-fills.md) | Backtest quotes, fills and liquidity from candles; decisions read the as-of book | build |
| [0029](0029-source-access.md) | Source access: manual BLS import, hand-computed fee cases | build |
| [0030](0030-scoring-choices.md) | Scoring choices: abstentions as the market, intervals by event, the market price | build; checkpoint 6.1 |
| [0031](0031-state-transport.md) | Shared state goes through the MCP server in process, and directly in backtests | build; checkpoints 4.1 and 5.1 |
| [0032](0032-demo-effective-stage.md) | Demo mode always runs at the approval stage | build; checkpoint 2.1 |
| [0033](0033-build-defaults.md) | Defaults introduced while building | build; checkpoint 4.1 |
| [0034](0034-continued-episodes-reconcile.md) | A continued episode reconciles and never places an order | build |
| [0035](0035-marked-equity-for-the-stops.md) | The stops read marked equity; sizing's bankroll stays at cost | build; checkpoints 1.1 and 5.1 |
| [0036](0036-dollar-caps-are-ceilings.md) | The dollar caps are ceilings, checked against each call's worst case | build |
| [0037](0037-price-move-triggers-anchored.md) | Price-move triggers are anchored to the event's last trigger | build; checkpoint 2.1 |
| [0038](0038-origins-by-text-overlap.md) | Origins are merged by text overlap, not vector similarity | build; checkpoints 2.1 and 3.1 |
| [0039](0039-order-outcome-time.md) | A backtest order's outcome has a time | build |
| [0040](0040-snapshot-content-digest.md) | The snapshot has a content digest | build |
| [0041](0041-backtest-stops.md) | A backtest applies the same stops as the live modes, and the report says when one tripped | build |
| [0042](0042-config-fields-have-readers.md) | Every configuration field has a reader | build |
| [0043](0043-trace-starting-state.md) | A saved trace's starting state is complete and as of the clock | build |
