# 0005. The protected slot is the only tail rule

The probability floor is removed; the decision-aware protected slot decides which unlikely scenario gets a deeper look.

## Context

Checkpoint 4.1 pruned scenarios whose probability fell below 0.03, while its risk section deleted a scenario only for failing the rules or calendar check and kept a low-scoring one near a floor. The instructors' feedback on checkpoint 4.1 pointed out that the two sections differ, and asked how the agent would decide to keep an unlikely scenario that could matter a great deal. Checkpoint 5.1 kept the best three scenarios "plus any protected outlier scenarios", without saying how one is chosen. The specification keeps one tail rule. Because scenarios outside the beam are frozen and still counted ([0006](0006-frozen-not-discarded.md)), unlikely scenarios stay in the forecast without a floor; the open question is only which of them deserves another level of search.

## Decision

There is no probability floor. After the beam is chosen, each scenario outside it is removed in turn, the forecast is re-aggregated and sizing re-run. A scenario whose removal flips the trade's direction, changes its cost by more than one risk unit, or pushes a tradable market to exactly 0 or 1 is a candidate; the candidate that moves the size most is expanded (`_protected` in [engine.py](../../src/pmfa/forecast/engine.py)). The rule works across all markets of the event.

## Consequences

Tail scenarios get attention when they matter to the trade, not by probability alone. When the protected scenario moved the size by more than one risk unit, the forecast says so and the trade cannot be routed to auto.

## Origin

Checkpoints 4.1 and 5.1, and the instructors' feedback on checkpoint 4.1; spec, Further Notes, "Changes from the checkpoint documents", item 5. The test that decides protection, whether removing a scenario changes the trade, is the spec's, and it is the answer to that feedback.
