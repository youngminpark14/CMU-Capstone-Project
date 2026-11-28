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
| `fast_path` | `claude-sonnet-5-5` | low | Two short calls, one that writes the scenarios and one that scores them; low effort keeps them quick when an event closes within the hour. Its scoring therefore runs on Sonnet 5.5 at low effort, while depth-1 scoring elsewhere runs on Opus 5.5 at high effort. |
| `monitor` | `claude-haiku-5-5` | low | A match decision over a short list of candidates. |
| `researcher` | `claude-haiku-5-5` | medium | Query plans and passage selection. |

Fable 5.2 stays in the price catalog so a slot can still be pointed at it.

## Consequences

Answers cached under the old assignments are not reused for the changed slots, so the next backtest pays for them again; per call, Opus costs half of what Fable did and Sonnet twenty times Haiku. Evaluation runs made before this change (the runs of 27 and 28 November 2026) describe the old assignments and are not comparable call for call. Whether the stronger Generator or the Opus Critic improves forecasts has not been measured.

## Origin

Build, November 2026. The owner's instruction of 28 November 2026. Checkpoint 4.1's role table names Sonnet 5.5 for the Generator, Fable 5.1 for the Critic at depth 1 and Opus 5.5 for the deeper Critic; its diagram names Haiku 5.5 for the Generator, Fable 5.1 at depth 1 and Sonnet 5.5 deeper ([checkpoints](../checkpoints.md#model-assignments), [0008](0008-depth1-critic-fable-5-2.md)). The Generator here follows the role table. The owner's instruction replaced Fable 5.1 at depth 1 with Opus 5.5, and the deeper Critic keeps Sonnet 5.5, as in 4.1's diagram.
