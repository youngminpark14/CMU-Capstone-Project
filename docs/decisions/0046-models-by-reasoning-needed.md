# 0046. Models are assigned by the reasoning a role needs; no slot uses Fable

Opus 5.5 does the reasoning, Sonnet 5.5 the middle work, Haiku 5.5 the sorting. Supersedes [0008](0008-depth1-critic-fable-5-2.md).

## Context

The defaults put the depth-1 Critic and the evaluation baselines on Fable 5.2 ($8 and $40 per million input and output tokens), the deeper Critic on Sonnet 5.5, and everything else on Haiku 5.5. The project's owner asked that Fable not be used, that Opus 5.5 ($4 and $20) be the only model for the reasoning, and that Sonnet and Haiku be given the remaining roles by how much reasoning each needs.

## Decision

| Slot | Model | Effort | Why |
| --- | --- | --- | --- |
| `critic_depth1` | `claude-opus-5-5` | high | First-level scoring sets the probabilities every deeper level is conditional on. |
| `baseline` | `claude-opus-5-5` | high | The baselines must use the strongest model in the pipeline, or the comparison flatters the pipeline. |
| `critic_deep` | `claude-sonnet-5-5` | high | Deeper scoring splits one already-scored scenario; it is narrower and runs more often. |
| `generator` | `claude-sonnet-5-5` | medium | Writing distinct, exhaustive scenarios needs more than sorting; checkpoint 4.1's role table also names Sonnet 5.5 here. |
| `fast_path` | `claude-sonnet-5-5` | low | One short call that writes and scores; low effort keeps it quick when an event closes within the hour. |
| `monitor` | `claude-haiku-5-5` | low | A match decision over a short list of candidates. |
| `researcher` | `claude-haiku-5-5` | medium | Query plans and passage selection. |

Fable 5.2 stays in the price catalog so a slot can still be pointed at it.

## Consequences

Answers cached under the old assignments are not reused for the changed slots, so the next backtest pays for them again; per call, Opus costs half of what Fable did and Sonnet twenty times Haiku. Evaluation runs made before this change (the runs of 27 and 28 November 2026) describe the old assignments and are not comparable call for call. The sample dry-run output in [evaluation.md](../evaluation.md) was recorded under the old assignments. Whether the stronger Generator or the Opus Critic improves forecasts has not been measured.

## Origin

Build, November 2026. The owner's instruction of 28 November 2026. Checkpoint 4.1 names Sonnet 5.5 for the Generator and Opus 5.5 for the Critic in its role table; this follows that table at depth 1 and keeps Sonnet 5.5 deeper.
