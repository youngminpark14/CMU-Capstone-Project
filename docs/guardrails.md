# Guardrails and human oversight

The design rests on one rule: **models read and judge; code acts.** No LLM role can retrieve a
document, run a check, compute a size, or place an order. Each role returns one validated record,
and code decides what to do with it. On top of that rule sit hard limits in code, limits on the
exchange side, and a human who approves trades until the record justifies otherwise.

This page lists every guardrail, where it is enforced, and the test that shows it. Terms are defined
in the [glossary](glossary.md); the pipeline is drawn in [architecture.md](architecture.md).

## Every guardrail, where it is enforced, and its test

"Code" means the check runs in this repository. "Exchange" means Kalshi enforces it on its side.
"Human" means a person decides. Test names are functions in the files linked.

| Guardrail | Enforced by | Where | Shown by |
| --- | --- | --- | --- |
| Roles have no tools: one input record in, one output record out | Code | [crewai_runner.py](../src/pmfa/models/roles/crewai_runner.py), [gateway.py](../src/pmfa/models/gateway.py) | `test_one_llm_call_per_kickoff_and_the_record_comes_back_unchanged` in [test_crewai.py](../tests/models/test_crewai.py) |
| Evidence text reaches a model only inside delimited blocks, outside the input JSON | Code | [prompts.py](../src/pmfa/models/roles/prompts.py) | `test_passage_text_is_outside_the_json_inside_delimited_blocks`, `test_the_trigger_excerpt_is_delimited_too` in [test_prompts.py](../tests/models/test_prompts.py) |
| No role sees the price of the market being forecast | Code | [outcomes.py](../src/pmfa/forecast/outcomes.py) (`event_brief`), [pipeline.py](../src/pmfa/episode/pipeline.py) (`trigger_brief`) | `test_event_brief_carries_shared_rules_and_no_prices` in [test_outcomes.py](../tests/forecast/test_outcomes.py); `test_a_price_move_brief_carries_none_of_the_prices_in_its_trigger`, `test_a_scheduled_re_check_brief_carries_nothing_from_its_trigger_detail`, `test_no_role_in_a_price_move_episode_is_shown_the_prices_in_its_trigger` in [test_briefs.py](../tests/episodes/test_briefs.py) |
| The Critic never sees the Generator's rationale | Code | [tree.py](../src/pmfa/forecast/tree.py) (`scenario_brief`) | `test_the_critic_input_never_contains_the_generators_rationale` in [test_engine.py](../tests/forecast/test_engine.py) |
| Every model answer is validated; one repair retry, then exit `error` with no order | Code | [gateway.py](../src/pmfa/models/gateway.py), [pipeline.py](../src/pmfa/episode/pipeline.py) | `test_malformed_twice_is_a_model_output_error` in [test_gateway.py](../tests/models/test_gateway.py); `test_15_malformed_model_output_exits_error_with_no_order` in [test_scenarios.py](../tests/episodes/test_scenarios.py) |
| Nothing published or priced after the as-of time is read, except the candle that prices a backtest fill | Code | [knowledge/index.py](../src/pmfa/knowledge/index.py), [world/recorded.py](../src/pmfa/world/recorded.py), [snapshot/reader.py](../src/pmfa/world/snapshot/reader.py) | `test_canary_after_as_of_is_never_retrieved`, `test_fractional_second_after_as_of_is_excluded`, `test_an_as_of_time_with_a_fraction_of_a_second_reads_its_second_and_not_the_next`, `test_a_passage_published_later_in_the_as_of_times_own_second_is_not_read` in [test_retriever.py](../tests/knowledge/test_retriever.py); `test_13_a_canary_document_dated_after_the_as_of_time_is_never_retrieved` in [test_scenarios.py](../tests/episodes/test_scenarios.py) |
| A backtest fill counts only from the end of the candle that priced it: the paper account, the equity points and the stops that read orders count an order only from its outcome time, and the trading pass starts no episode before the latest order's outcome time | Code | [snapshot/book.py](../src/pmfa/world/snapshot/book.py), [paper.py](../src/pmfa/execution/paper.py), [halt.py](../src/pmfa/execution/halt.py), `_trading_pass` in [evaluation/backtest.py](../src/pmfa/evaluation/backtest.py) | `test_the_fill_is_timed_at_the_end_of_its_candle_and_not_seen_before_then` in [test_fill_prices.py](../tests/evaluation/backtest/test_fill_prices.py); `test_a_fill_from_a_later_book_counts_only_from_that_books_time` in [test_paper.py](../tests/execution/test_paper.py); `test_fill_failures_count_an_order_only_once_its_outcome_is_known`, `test_a_source_is_rechecked_only_for_orders_whose_outcome_is_known`, `test_equity_points_after_now_alone_are_nothing_to_measure_a_stop_from` in [test_halt.py](../tests/execution/test_halt.py); `test_the_second_episode_starts_once_the_first_fill_was_priced` in [test_trading_pass.py](../tests/evaluation/backtest/test_trading_pass.py); `test_a_second_decision_at_the_same_instant_reads_nothing_of_the_first_order` in [test_scoring_same_instant.py](../tests/evaluation/backtest/test_scoring_same_instant.py); `test_the_second_trace_at_a_shared_instant_holds_nothing_after_its_as_of_time_and_replays` in [test_trace_same_instant.py](../tests/evaluation/backtest/test_trace_same_instant.py) |
| Cached answers cannot carry later knowledge (the as-of time is in the cache key) | Code | [cache.py](../src/pmfa/models/cache.py) | `test_as_of_is_part_of_the_cache_key` in [test_gateway.py](../tests/models/test_gateway.py) |
| `pmfa report --publish` refuses a run answered by supplied models (scripted answers or a rehearsal), so it is not published as a result; only a direct `publish_run(..., allow_supplied=True)` call, as tests make, publishes one | Code | `publish_run` in [report.py](../src/pmfa/evaluation/report.py), called by `pmfa report --publish` in [cli/backtest.py](../src/pmfa/cli/backtest.py) | `test_a_run_answered_by_supplied_models_is_not_published_as_a_result` in [test_parts.py](../tests/evaluation/backtest/test_parts.py) |
| A trade needs one primary origin or two independent secondary origins | Code | [confidence.py](../src/pmfa/decision/confidence.py), checked in research and again in the gate | `test_02_one_secondary_origin_syndicated_twice_is_insufficient_origins` |
| Scenarios citing missing, late or uncounted passages are deleted | Code | [checks.py](../src/pmfa/forecast/checks.py) | `test_evidence_check` in [test_levels_and_checks.py](../tests/forecast/test_levels_and_checks.py) |
| Size never exceeds a cap; zero below the minimum edge; never grows when confidence falls | Code | [sizing.py](../src/pmfa/decision/sizing.py) | property tests in [test_sizing_properties.py](../tests/decision/test_sizing_properties.py) |
| A re-forecast of an event already held sizes against the holding and trades only the difference to its new target; the same forecast trades nothing | Code | `size_event` in [sizing.py](../src/pmfa/decision/sizing.py), holdings read by `_sizing_inputs` in [pipeline.py](../src/pmfa/episode/pipeline.py) | `test_a_re_forecast_of_a_held_event_with_the_same_forecast_sends_no_second_order`, `test_a_re_forecast_of_a_held_event_buys_only_the_difference_to_its_new_target` |
| The gate re-verifies every limit and lists every failed check | Code | [gate.py](../src/pmfa/decision/gate.py) | `test_each_check_fails_alone` in [test_gate.py](../tests/decision/test_gate.py); `test_07_a_proposal_over_a_cap_is_gate_rejected_with_no_retry` in [test_scenarios.py](../tests/episodes/test_scenarios.py) |
| The gate never returns or changes an order | Code | [gate.py](../src/pmfa/decision/gate.py), [records/decision.py](../src/pmfa/records/decision.py) | `test_the_gate_never_returns_a_modified_order` in [test_gate.py](../tests/decision/test_gate.py) |
| The caps agree with each other; the per-order cap stays below the order group's limit | Code | [config.py](../src/pmfa/config.py) | `test_per_order_cap_must_stay_below_the_exchange_breaker` in [test_contracts.py](../tests/test_contracts.py) |
| Every setting has a reader, and an unknown setting is refused | Code | `extra="forbid"` on every section in [config.py](../src/pmfa/config.py) | `test_every_configuration_field_is_read_somewhere` in [test_config_fields.py](../tests/test_config_fields.py); `test_unknown_configuration_keys_are_rejected` in [test_contracts.py](../tests/test_contracts.py) |
| Limit orders only, immediate-or-cancel only, at valid ticks | Code | [records/decision.py](../src/pmfa/records/decision.py), [gate.py](../src/pmfa/decision/gate.py), [demo.py](../src/pmfa/world/kalshi/demo.py) | `limit_order`, `immediate_or_cancel` and `valid_tick` cases of `test_each_check_fails_alone` in [test_gate.py](../tests/decision/test_gate.py); `test_buying_yes_is_a_bid_at_the_limit_price` in [test_demo.py](../tests/world/kalshi/test_demo.py) |
| The price is re-read before sending; nothing offered at the limit means no order | Code | [pipeline.py](../src/pmfa/episode/pipeline.py) (`execute`) | `test_08_the_price_moves_past_the_edge_before_execution_and_no_order_is_sent`, `test_08_after_approval_a_price_above_the_limit_exits_stale_price` |
| A retry or second resume cannot double an order | Code | deterministic client order ids in [pipeline.py](../src/pmfa/episode/pipeline.py), [paper.py](../src/pmfa/execution/paper.py), [demo.py](../src/pmfa/world/kalshi/demo.py) | `test_resuming_twice_does_not_place_a_second_order`, `test_a_first_run_finds_an_order_already_placed_under_its_client_order_id_and_sends_no_other` in [test_controller.py](../tests/episodes/test_controller.py); `test_a_duplicate_client_order_id_is_looked_up_not_resent` in [test_demo.py](../tests/world/kalshi/test_demo.py) |
| An episode continued after a crash or an exchange error looks its order up by client order id and never places one | Code | [controller.py](../src/pmfa/episode/controller.py), `_reconcile` in [pipeline.py](../src/pmfa/episode/pipeline.py), `find_order` in [paper.py](../src/pmfa/execution/paper.py) and [demo.py](../src/pmfa/world/kalshi/demo.py) | `test_a_continued_episode_that_died_before_sending_sends_nothing`, `test_a_continued_episode_that_died_after_sending_records_its_order_and_sends_no_other`, `test_a_continuation_finds_an_order_only_the_exchange_holds_and_sends_no_other`, `test_a_continuation_whose_order_lookup_fails_sends_nothing_and_waits_with_the_outcome_unknown` in [test_controller.py](../tests/episodes/test_controller.py) |
| An order the exchange rejects ends the episode `error`, not `executed`; an order whose outcome is unknown (the exchange failed while sending, and the lookup by client order id fails or finds nothing) leaves the episode and the proposal `outcome_unknown`, with no exit and no rejection recorded; an unknown outcome that the demo exchange answers has no such order is a rejection | Code | `_order_outcome` and `execute` in [pipeline.py](../src/pmfa/episode/pipeline.py), `place` in [demo.py](../src/pmfa/world/kalshi/demo.py) | `test_an_order_the_exchange_rejected_ends_in_error_and_the_proposal_is_cancelled`, `test_an_exchange_error_while_sending_with_no_order_found_waits_with_the_outcome_unknown` in [test_scenarios.py](../tests/episodes/test_scenarios.py); `test_an_unknown_outcome_whose_lookup_fails_raises_instead_of_rejecting`, `test_a_duplicate_client_order_id_whose_lookup_fails_raises_instead_of_rejecting`, `test_an_unknown_outcome_whose_lookup_answers_with_no_order_is_rejected` in [test_demo.py](../tests/world/kalshi/test_demo.py) |
| The halt flag blocks every order, in every mode | Code | the gate; `execute` at its start and again immediately before sending; [paper.py](../src/pmfa/execution/paper.py); [demo.py](../src/pmfa/world/kalshi/demo.py) before the order and again before it is sent | `test_10_halt_flag_set_means_no_order_in_any_mode`, `test_10_a_halt_set_during_the_review_blocks_the_approved_order`, `test_10_b_a_halt_set_after_the_gate_approved_stops_the_order_before_it_is_sent`, `test_10_c_a_halt_set_while_execute_reads_the_book_stops_the_order`, `test_10_d_a_halt_set_between_the_gate_and_execute_stops_before_the_book_is_read` in [test_scenarios.py](../tests/episodes/test_scenarios.py); `test_halted_orders_are_refused_and_not_stored` in [test_paper.py](../tests/execution/test_paper.py); `test_a_halted_executor_sends_no_order`, `test_the_halt_flag_is_read_again_right_before_the_order_is_sent` in [test_demo.py](../tests/world/kalshi/test_demo.py) |
| `pmfa halt` with no `--mode` halts every live mode, setting every flag before it contacts any exchange | Code | [cli/oversight.py](../src/pmfa/cli/oversight.py) | `test_halt_without_a_mode_stops_every_live_mode`, `test_a_halt_sets_every_flag_before_any_exchange_and_survives_missing_demo_keys` in [test_operations.py](../tests/cli/test_operations.py) |
| The agent halts itself on a stop condition | Code | [halt.py](../src/pmfa/execution/halt.py), run before the gate and on every live pass | [test_halt.py](../tests/execution/test_halt.py); `test_a_self_halt_condition_before_the_gate_halts_and_sends_nothing` in [test_scenarios.py](../tests/episodes/test_scenarios.py); a changed or withdrawn live source: [test_live.py](../tests/world/evidence/test_live.py); rejected demo orders count as fill failures: `test_repeated_demo_rejections_trip_the_fill_failure_halt` in [test_demo.py](../tests/world/kalshi/test_demo.py) |
| The daily loss stop and the drawdown halt count unrealized losses (equity is marked at the bid) | Code | `marked_equity` in [portfolio.py](../src/pmfa/execution/portfolio.py), `mark` in [paper.py](../src/pmfa/execution/paper.py) and [demo.py](../src/pmfa/world/kalshi/demo.py), [live/loop.py](../src/pmfa/live/loop.py) | `test_equity_marks_value_holdings_at_their_own_bid`, `test_the_drawdown_halt_sees_an_unrealized_loss` in [test_paper.py](../tests/execution/test_paper.py); `test_mark_values_positions_at_their_own_sides_bid_and_records_the_point` in [test_demo.py](../tests/world/kalshi/test_demo.py); `test_a_pass_ends_with_a_marked_equity_point_not_one_at_cost` in [test_loop.py](../tests/live/test_loop.py) |
| The daily loss stop is a fraction of the day's starting equity; the dollar caps use the current equity, as sizing does | Code | [risk.py](../src/pmfa/decision/risk.py), [gate.py](../src/pmfa/decision/gate.py) | `test_the_caps_use_the_current_equity_and_the_loss_stop_the_day_start_equity` in [test_gate.py](../tests/decision/test_gate.py); `test_an_explicit_bankroll_is_the_caps_base_and_does_not_move_the_stop` in [test_risk.py](../tests/decision/test_risk.py); `test_the_daily_loss_stop_is_a_fraction_of_the_day_start_equity` in [test_scenarios.py](../tests/episodes/test_scenarios.py) |
| A tripped daily loss stop stays tripped until the next trading day | Code | [risk.py](../src/pmfa/decision/risk.py) | `test_a_tripped_loss_stop_stays_tripped_for_the_day` in [test_risk.py](../tests/decision/test_risk.py) |
| Blocklisted and unfollowed series are never traded | Code | gate; the protected slot ignores blocked markets | `blocklist` and `followed_series` cases of `test_each_check_fails_alone` in [test_gate.py](../tests/decision/test_gate.py) |
| Trades go to a human unless every auto condition holds at the autonomous stage | Code, then human | [routing.py](../src/pmfa/decision/routing.py) | `test_would_be_auto_goes_to_review_below_the_autonomous_stage` in [test_routing.py](../tests/decision/test_routing.py) |
| The human approves, resizes smaller, or rejects with a reason code | Human, checked by code | [review.py](../src/pmfa/execution/review.py) | `test_resize_only_smaller`, `test_reject_needs_a_reason_code` in [test_review.py](../tests/execution/test_review.py) |
| A stale proposal (10 minutes, or price past its edge bound) never executes | Code | [review.py](../src/pmfa/execution/review.py), [controller.py](../src/pmfa/episode/controller.py) | `test_09_a_proposal_expired_by_time_never_executes`, `test_09_a_proposal_expired_by_price_never_executes` |
| Approval re-runs the gate at the current price | Code | [pipeline.py](../src/pmfa/episode/pipeline.py) (`review` goes back to `gate`) | `test_09_approval_re_runs_the_gate_at_the_current_price`, `test_08_after_approval_a_price_above_the_limit_exits_stale_price`, `test_10_a_halt_set_during_the_review_blocks_the_approved_order` |
| Real orders need at least the approval stage; demo mode always runs at the approval stage, so a person approves every demo order | Code | gate `stage` check; `_stage` in [pipeline.py](../src/pmfa/episode/pipeline.py) | `test_real_orders_outside_demo_mode_need_the_stored_approval_stage`, `test_demo_mode_runs_at_the_approval_stage_whatever_the_stored_stage`, `test_every_demo_order_waits_for_a_human_whatever_the_stored_stage` |
| A stage advances only on its benchmark and drops after two loss stops in a week; every pass of the live loop applies the drop | Code | [stages.py](../src/pmfa/execution/stages.py), [benchmarks.py](../src/pmfa/evaluation/benchmarks.py), [live/loop.py](../src/pmfa/live/loop.py) | [test_stages.py](../tests/execution/test_stages.py); `test_a_pass_applies_the_stage_demotion_check` in [test_loop.py](../tests/live/test_loop.py) |
| The production adapter cannot trade | Code | [production.py](../src/pmfa/world/kalshi/production.py) | [test_production.py](../tests/world/kalshi/test_production.py) |
| The demo executor talks only to the demo host, with signed requests | Code | [demo.py](../src/pmfa/world/kalshi/demo.py) | `test_it_refuses_any_host_but_the_demo_one`, `test_the_factory_refuses_a_production_url_in_the_configuration` in [test_demo.py](../tests/world/kalshi/test_demo.py) |
| Orders, balance and positions are scoped to one numbered subaccount | Code and exchange | [demo.py](../src/pmfa/world/kalshi/demo.py); the key's restriction is set on Kalshi | `test_balance_and_positions_are_for_the_configured_subaccount` in [test_demo.py](../tests/world/kalshi/test_demo.py) (code side only) |
| The key cannot transfer funds or manage subaccounts | Exchange | a subaccount-restricted key, created on Kalshi | not tested here |
| More than 50 contracts in 15 seconds cancels the group's orders and blocks new ones | Exchange | order group joined in [demo.py](../src/pmfa/world/kalshi/demo.py) | `test_an_order_group_is_created_once_when_missing` in [test_demo.py](../tests/world/kalshi/test_demo.py) (code side only) |
| Each episode is capped in calls, dollars and seconds; each run in dollars. The dollar caps are ceilings: a call whose worst case would pass one is not sent | Code | [meter.py](../src/pmfa/models/meter.py), [gateway.py](../src/pmfa/models/gateway.py) | `test_the_call_cap_stops_before_calling`, `test_the_episode_dollar_cap_is_a_ceiling`, `test_a_repair_retry_also_needs_dollar_budget_for_its_worst_case`, `test_an_enforced_seconds_cap_bounds_each_calls_timeout_by_the_time_left`, `test_the_run_dollar_cap_raises_run_budget_exhausted` in [test_gateway.py](../tests/models/test_gateway.py) |
| Every episode ends in exactly one exit, `error` for any project error a node raises; a read past the as-of time aborts the run instead | Code | `_guarded` in [pipeline.py](../src/pmfa/episode/pipeline.py) | `test_every_exit_writes_exactly_one_episode_record`, `test_an_unknown_fee_type_ends_the_episode_in_error_with_no_order`, `test_13_an_as_of_violation_from_a_port_aborts_the_run_instead_of_ending_the_episode` |
| Secrets come from the environment only and are never logged or stored | Code | [backend.py](../src/pmfa/models/backend.py), [signing.py](../src/pmfa/world/kalshi/signing.py) | `test_the_key_is_read_from_the_environment_at_call_time` in [test_backend.py](../tests/models/test_backend.py); `test_it_never_reads_the_credential_variables` in [test_production.py](../tests/world/kalshi/test_production.py) |
| Money is decimal, never float | Code | [records/base.py](../src/pmfa/records/base.py), [fees.py](../src/pmfa/decision/fees.py) | `test_money_is_never_a_float` in [test_fees.py](../tests/decision/test_fees.py) |

A cell that links no test file names tests in
[tests/episodes/test_scenarios.py](../tests/episodes/test_scenarios.py).

## Why no role can move money

**Record-only roles.** The Monitor, Researcher, Generator and Critic are prompt and schema
definitions. Each call sends one input record and gets back one output record that must validate
against a closed JSON schema. Under CrewAI, each role is an agent with no tools, no delegation, no
memory and one iteration; the adapter raises if CrewAI ever offers it a tool, and the runner raises
if a kickoff makes more than one model call. The Critic's request for more evidence is a text field
in its output: code decides whether to run it, runs the search, and has the Researcher filter the
results.

**Untrusted evidence in delimited blocks.** Passage text and the trigger's excerpt are moved out of
the input JSON into `<evidence>` blocks. Each block's tag is a hash of its own text, so a passage
cannot close its own block. Template placeholders that CrewAI would expand are defused. The prompt
states that evidence is data, never instructions.

**No market price shown to a role.** The event brief a role reads carries the rules, the options
or strikes, and the close time. A price-move trigger reaches the roles as "The event's market prices
moved", with no number. The forecast is formed without the price, and code compares it with the
price afterwards. One exception is deliberate: the Critic is shown up to five *resolved* forecasts
from the ledger as calibration examples, each with the market price it had at the time.

Because no role acts, an injected passage can at most distort a probability. The caps below bound
what a distorted probability can cost.

## The gate and the caps

Sizing ([sizing.py](../src/pmfa/decision/sizing.py)) proposes; the gate
([gate.py](../src/pmfa/decision/gate.py)) decides. The gate does not call sizing code. It recomputes
the shrunk probability, the fee, the edge-preserving price bound (in closed form, where sizing
searches the ticks), and every exposure from the context it is handed. A bug in one is unlikely to
repeat in the other. The gate returns a verdict with every check and its detail; its result has no
order field, and the proposal is a frozen record.

Before the gate runs, the same node runs the self-halt conditions (see [Halt](#halt-the-command-and-the-self-halt-conditions)). The gate's
checks, in order:

| Check | Passes when |
| --- | --- |
| `context_matches` | The market, book and series handed in are the proposal's. |
| `halt` | The halt flag is not set. |
| `stage` | The executor is simulated, or the stage is approval or higher. |
| `blocklist` | Neither the series, the event nor the market is blocklisted. |
| `followed_series` | The series is followed and enabled. |
| `origin_rule` | One primary origin, or two independent secondary origins. |
| `liquidity_floor` | At least $500 rests within 2¢ of the touch on the proposal's side (backtests: dollars traded in the trailing 60 minutes). |
| `positive_size` | At least one contract. |
| `contracts_per_order` | At most 25 contracts. |
| `dollars_per_market` | Exposure in the market after the order, at cost plus fee, is at most 5% of bankroll. |
| `dollars_per_event` | Exposure in the event after the order is at most 10% of bankroll. |
| `daily_loss_stop` | Marked equity has not fallen 3% of the day's starting equity below that start at any moment today. |
| `drawdown_halt` | Marked equity is less than 10% below its peak. |
| `limit_order` | The order is a limit order. |
| `immediate_or_cancel` | Its time in force is immediate-or-cancel. |
| `valid_tick` | The limit price is a valid tick for the market. |
| `edge_bound` | The limit price is at or below the highest price that keeps the minimum edge. |
| `min_edge` | The edge after the order's rounded fee is at least 2¢. |
| `not_expired` | The proposal's 10 minutes have not run out. |

A rejected proposal ends the episode `gate_rejected` (or `halted`, when the halt check failed). It
is never retried, at any size.

**Two bases for the money checks.** The bankroll the dollar caps are fractions of is the current
equity: cash plus open holdings at cost, the same number sizing caps on, so the gate never rejects
at a cap what sizing put exactly at it. Equity for the daily loss stop and the drawdown halt is
*marked*: cash plus each open holding at the current best bid on its own side (what it could be
sold for now), or at cost when that side has no bid, so both stops count unrealized losses as well
as fees and settled losses. The paper executor records a marked equity point at every fill and
settlement, and the live loop records one at the end of every pass. Each point values the account
as of its own time, and a backtest fill's point is written at the fill's outcome time, the end of
the candle that priced it. The daily loss stop is a
fraction of the day's starting equity (the last equity point before the trading day began, else the
day's first point), so one day has one stop level however equity moves during it.

Defaults, all in [config.py](../src/pmfa/config.py):

| Setting | Default |
| --- | --- |
| Kelly fraction | 0.25 |
| Minimum edge after fees | $0.02 per contract |
| Shrinkage toward the market | 0.5 per series |
| Per market | 5% of bankroll |
| Per event | 10% of bankroll |
| Per order | 25 contracts |
| Daily loss stop | 3% of the day's starting equity |
| Drawdown halt | 10% below peak equity |
| Liquidity floor | $500 within $0.02 of the touch |
| Risk unit | 1% of bankroll |
| Notional bankroll (backtest, shadow) | $1,000 |
| Proposal expiry | 10 minutes |

The configuration refuses to load when the caps contradict each other: the per-market cap above the
per-event cap, the daily stop above the drawdown limit, or the per-order cap at or above the order
group's 50-contract limit.

## Exchange-side limits in demo mode

In demo mode, orders go to Kalshi's demo exchange through the demo executor
([demo.py](../src/pmfa/world/kalshi/demo.py)). Two of its limits sit on Kalshi's side, so they hold
even if this repository's code were wrong: the key's restriction and the order group. The others are
in code:

- **One subaccount.** Every order, balance, position and cancel request names the configured
  numbered subaccount (default 1). The key itself is meant to be created on Kalshi restricted to
  that subaccount; Kalshi documents that a restricted key cannot transfer funds or manage
  subaccounts or keys. `kalshi.demo_subaccount = 0` names the account's main balance instead,
  where a new demo account's mock funds are; the exchange-side bound does not apply then.
- **An order group as a breaker.** Every order joins an order group with a limit of 50 contracts over
  Kalshi's rolling 15-second window. Hitting it cancels the group's resting orders and rejects new
  ones until the group is reset, and this executor never resets it. Because one order is capped at
  25 contracts, a single order never trips it; a burst does.
- **Immediate-or-cancel limit orders only.** The order payload always sets
  `time_in_force: immediate_or_cancel` and a limit price. Nothing rests, so the worst case of an order
  is its own size at its own limit.
- **Demo host only.** The executor refuses any base URL that is not a Kalshi demo host.

These exchange-side behaviours are taken from Kalshi's documentation and OpenAPI document. The
tests for them use responses built from those schemas. They have not yet been checked against a live
demo account in this repository.

## The review queue

A proposal routed to the approve tier waits for a person ([review.py](../src/pmfa/execution/review.py)).

**The card** shows the event and the market, the side and size, the limit price and the worst-case
cost, the forecast probability of the side against its best ask, the edge, the confidence, the
first three passages with source, title, publish time and a 40-word excerpt, the routing reasons
(the auto conditions that failed, or "would be auto at the autonomous stage"), and the expiry time.

**Three choices:**

- **approve**: the proposal goes ahead as sized;
- **resize**: a smaller whole number of contracts; fee, cost and edge are recomputed. A review can
  shrink a trade, never grow it;
- **reject**: needs a reason code: `stale_evidence`, `wrong_market`, `size_too_large`,
  `disagree_with_forecast`, or `other`, with an optional note.

The `pmfa review` commands list pending proposals, show a card, and record each choice. A proposal
whose order outcome is unknown, whose order may already have been sent, or whose episode has ended
cannot be decided: the command says which, records nothing, and exits 1.

A resize or a reject is recorded on the ledger entry as an override, so overrides become data.

**Expiry.** A proposal expires 10 minutes after it was sized, or as soon as the best ask on its side
is above its edge bound. An expired proposal cannot be decided and never executes; the episode ends
`expired`. Expiry is checked when a review arrives, and on every pass of the live loop.

**The gate runs again on approval,** against the book an order would meet now, after the self-halt
conditions. A halt set while the proposal waited blocks it. The pause and the order are separate
graph nodes, so resuming never repeats a side effect; see
[the human-approval pause](architecture.md#the-human-approval-pause).

## Sending an order

The `execute` node ([pipeline.py](../src/pmfa/episode/pipeline.py)) sends one immediate-or-cancel
limit order with a deterministic client order id, and first looks that id up (in the state store,
then on the executor with `find_order`), so a retry finds the order instead of sending a second one.

- **Rejected.** An order the exchange rejects ends the episode `error` with the exchange's reason,
  and the proposal is cancelled. An order the exchange accepted ends it `executed`, whether it
  filled fully, partly or not at all.
- **Exchange failure while sending.** The order is looked up by its client order id. When it is
  found it is recorded. When it is not, or the lookup itself fails, the outcome is unknown: the
  episode and the proposal are saved `outcome_unknown`, with no exit and no rejection recorded,
  because an order may stand on the exchange. Nothing more is sent for that proposal. Each live
  pass, and `pmfa reconcile`, looks the order up again: found, it is recorded and the episode ends
  by what the exchange did; the exchange lists none, the proposal is cancelled and the episode ends
  `error`; the exchange cannot be asked, the episode stays `outcome_unknown`. The demo executor
  makes the same lookup when an order's outcome is unknown (no response, a 408 or a 5xx) or the
  exchange answers `order_already_exists`: it returns an order it finds, and the episode records
  it; it returns `REJECTED` when the exchange answers that none exists; and it raises when the
  lookup fails, so the episode waits as above.
- **Continued episodes.** When the process stopped at `execute` (a crash, or an error that escaped
  after the approval), the episode stays unfinished, and whatever continues it (`run` of the same
  trigger, a review, an expiry, or `pmfa reconcile --episode ID`) only reconciles: it looks the
  order up by its client order id and records what it finds, and it never places an order. When no
  order exists, the proposal is cancelled and the episode ends `error` with nothing placed; when
  the exchange cannot be asked, the episode waits `outcome_unknown`. The approval behind the
  stopped episode is stale (its gate ran at an older price, and the human may since have rejected
  it), so it is never acted on again. A live pass leaves an unfinished episode alone, because
  another process may be in it; `pmfa report-status` lists it with the command that settles it.

## Halt: the command and the self-halt conditions

The halt operation ([halt.py](../src/pmfa/execution/halt.py), `halt()`) sets a persisted flag in the
state database, with a reason and a time, and cancels every open order. The flag survives restarts.
It is read at the gate, at the start of the `execute` node, again immediately before the order is
sent, and by the executor itself: the paper executor refuses an order while it is set, and the demo
executor reads it when an order starts and again just before the request goes out. `resume()` (or
`pmfa halt --resume`) clears it; nothing clears it automatically.

The `pmfa halt` command calls the same operation and covers every live mode. It sets the flag in
the database first, so a halt never depends on reaching the exchange. Shadow and demo keep separate
state databases, so without `--mode` the command sets the flag in every live mode's database before
it contacts any exchange, then cancels each mode's open orders. A mode it cannot open (demo without its
credentials) is reported, and its flag stays set. `--mode shadow|demo` narrows the halt, and
`--resume` and `--status` follow the same rule.

The agent halts itself when any of these holds. They are checked at the start of the gate node, every
time a proposal reaches it, and on every pass of the live loop, after it has refreshed the indexes,
resolved outcomes and expired due proposals and before it detects triggers
([live/loop.py](../src/pmfa/live/loop.py)):

- **Daily loss stop:** marked equity fell 3% of the day's starting equity below that start at any
  moment today (the trading day is the calendar day in New York).
- **Drawdown:** marked equity is 10% or more below its peak.
- **Fill failures:** once 10 orders exist, more than one in ten of the last 10 did not fill or was
  rejected (two or more of ten). Each order the demo exchange refuses is stored under its own id,
  so repeated rejections count. An order counts only once its outcome is known (in a backtest,
  from its *outcome time*: the end of the candle the order met, an hour after its arrival when no
  candle was found, or its arrival when the market had closed; see the [glossary](glossary.md)).
- **Source changed or withdrawn:** a document cited by an episode that still holds an open position
  now has a different content hash, or is gone. In shadow and demo the stored copy of a document
  never changes, so the check fetches the cited page again from its source (at most once per poll
  interval per document); a 404 or 410 is a withdrawal, and a source that cannot be reached right
  now is not a change. As for fill failures, an order counts only once its outcome is known.
- **Calibration:** for a category, the calibration error of the pipeline's resolved ledger entries
  (one per market per forecast) is above 0.10 now, one week ago and two weeks ago, and each of those
  three checkpoints holds at least 30 entries.

Routing also sends a proposal to the halt tier when the halt flag, the loss stop or the drawdown
holds; it sets the flag if it is not set yet, and the episode ends `halted`.

A backtest applies the same stops, and nobody resumes a halt there: once one trips, every later
episode of that pass that reaches the gate ends `halted` and sends no order, and the report says
when each pass halted. The fill-failure and calibration stops are both likely to trip early in a
backtest's trading pass: [evaluation.md](evaluation.md#fill-assumptions-and-their-limits) counts how
many liquid market sides at the trading pass's news triggers had no contracts in the candle an order
would meet (86 of 204, 42%, at any price; 131, 64%, at or below the as-of ask an order's limit is
set to), and the market's own prices for the June 2026 CPI release have a calibration error of 0.140
over its 63 scored rows, so a pipeline no better calibrated would trip the calibration stop about
two weeks after that release closed. No backtest with a model has run, so both are expectations.

## Stages and the autonomy ladder

The stage is the autonomy level: backtest, shadow, approval, autonomous. It is stored in the state
database ([stages.py](../src/pmfa/execution/stages.py)). A stage advances one step at a time, and
only when the benchmark for the next stage holds, computed from the state store: the ledger's
scored forecasts for backtest to shadow, shadow-mode fills and settlements for shadow to approval,
and human reviews for approval to autonomous
([benchmarks.py](../src/pmfa/evaluation/benchmarks.py)):

| From, to | Benchmark |
| --- | --- |
| backtest to shadow | Brier skill above zero on at least 200 backtested markets, with abstentions scored as the market price |
| shadow to approval | Four weeks of shadow mode with positive simulated P&L after fees and maximum drawdown under 10% |
| approval to autonomous | At least 100 human-reviewed trades since entering approval, with under 15% resized or rejected |

Two days with a tripped daily loss stop within seven days demote one stage. Every pass of the live
loop applies this rule (`check_demotion` in [stages.py](../src/pmfa/execution/stages.py)), right
after the self-halt conditions; backtests and the episode pipeline do not. `pmfa stage status`,
`advance` and `demote` show the benchmarks and move the stage by hand;
`advance` refuses when the benchmark is not met. `pmfa backtest` keeps its ledger in its run
directory, so `status` and `advance` take `--from-run RUN_DIR` (repeatable) to count a run's
scored forecasts (`RUN_DIR/work/scoring.sqlite`) toward backtest to shadow.

The stage changes behaviour in two places. The gate refuses real (non-simulated) orders below the
approval stage. Routing honours the auto tier only at the autonomous stage; below it, a trade that
meets every auto condition still goes to review and is flagged *would-be-auto*. The flag is shown on
the review card and in the trace, and counted in the run reports and `pmfa report-status`, so a
reader can see which reviewed trades code alone would have placed. The approval benchmark does not
read it: it counts every human review since the stage was entered, and every resize or reject as an
override.

Demo mode always runs at the approval stage, whatever stage is stored: its funds are mock, so the
stage does not have to be earned there, and below the autonomous stage routing never honours the
auto tier, so a person approves every demo order. The stored stage is not changed.

The ladder governs progression toward real-money trading, which this project never does. It is kept
as stored state and benchmarks so the record that would justify autonomy can be shown.

## The read-only production adapter

Shadow mode reads live prices from Kalshi production through
[production.py](../src/pmfa/world/kalshi/production.py). Three things make it unable to trade, and
a test checks each: the class has no order, cancel, balance or portfolio methods; its client has no
signer and the class refuses one, so no request to production is ever authenticated; and the module
never reads the credential variables (it does not import the signing module). Shadow orders go to
the paper executor.

## Budgets per episode and per run

The gateway's meter ([meter.py](../src/pmfa/models/meter.py)) is checked before every model call,
so a capped call is never sent. A call's cost is known only after it returns, so the dollar caps are
checked against the call's worst case: an upper estimate of its input tokens plus its full output
token limit, priced on the assigned model. A call that could take either dollar cap past its limit
is refused, so the caps are ceilings, not thresholds that one last call can overshoot. A repair
retry is checked the same way. When the seconds cap is enforced, each call's timeout is the
configured request timeout or the seconds the episode has left, whichever is shorter.

| Cap | Default |
| --- | --- |
| Model calls per episode | 30 |
| Dollars per episode | $2.00 |
| Seconds of model time per episode | 90, live modes only |
| Dollars per run | $100.00 |

A cap reached during the tree search ends the search: the scenarios scored so far are aggregated
and confidence is halved. A cap reached before the search (in matching or research) ends the episode
`error`. The run cap ends the episode `error` and stops the run. A cached answer costs the run
nothing new; it is charged to the episode at its original cost, so a rerun meets the episode caps at
the same point.

## Secrets stay in the environment

Secrets are read from the environment only. The Anthropic key is read from `ANTHROPIC_API_KEY` at
call time and handed to the client, never stored on an object or logged. The Kalshi demo key id and
private key come from `KALSHI_DEMO_KEY_ID` and `KALSHI_DEMO_PRIVATE_KEY` (or a file named by
`KALSHI_DEMO_PRIVATE_KEY_PATH`). `KALSHI_DEMO_PRIVATE_KEY` can hold the PEM text, or the key's
base64 body alone. Key material is never logged, printed or put in a `repr`. The
configuration holds only the variable names. `.env`, key files, the database, the snapshot, the
corpus and the caches are ignored by git, and `.env.example` lists the variables.

## What is not guaranteed

- **An episode that died while its order was being sent is not swept up automatically.** A live
  pass cannot tell a dead process from one still sending, so it leaves the episode alone.
  `pmfa report-status` lists it; a person runs `pmfa reconcile --episode ID` once nothing else is
  working on it. An `outcome_unknown` episode whose checkpoint file has been deleted cannot be
  finished at all: the reconcile message names the client order id to look up on the exchange.
- **A wrong forecast can still lose money, up to the caps.** The caps bound one order to 25
  contracts, one market to 5% of bankroll, and one event to 10%; the daily stop and the drawdown
  halt stop further trading. Within those limits, a confident wrong forecast loses.
- **An injected passage can distort a probability.** Delimited blocks mark evidence as data, and no
  role can act on it, but a model can still be swayed by what a passage says. A passage written to
  mislead can move the Generator's claims or the Critic's numbers, and through them the size, within
  the caps.
- **The rules check is structural.** Code verifies that a scenario asserts outcomes that exist; it
  cannot verify that the claim's wording reads the rules correctly. That is left to the Critic, which
  can reject a scenario. The rules text a role reads is built from the exchange's wording: the
  words the markets' primary rules share, around a placeholder for the option (for the threshold
  on a ladder event), then each distinct secondary rules text once, as given. In the frozen
  snapshot, Kalshi's rules for the Fed decision markets of more than 25 bp have three spaces where
  the ">" sign belongs ("a Hike of   25bps"), while their subtitles carry it ("Hike >25bps"). A
  role meets this only in the October 2026 event's secondary rules ("Hike   25 bps"). The options
  it chooses among are labelled by code from each market's strike ("Hike more than 25 bp"), so
  they are not ambiguous.
- **The exchange-side protections are documented, not verified here.** The subaccount restriction,
  the funds-transfer block, and the order group's breaker are Kalshi's behaviour as its documentation
  describes it. No test in this repository has exercised them against a live demo account yet.
- **The self-halt conditions and proposal expiry are checked, not watched.** They run when a
  proposal reaches the gate, when a review arrives, and on each pass of the live loop. Between
  passes nothing checks them; a pending proposal still never executes without a review.
- **Shadow and backtest fills are simulated.** Backtest fills come from candles, not order-book depth,
  which the exchange's history does not have.
