# 0006. Scenarios outside the beam are frozen, not discarded

They stop being expanded and still count in the forecast.

## Context

A beam search that discards scenarios outside the beam renormalizes the forecast toward the likeliest path and understates the tails.

## Decision

A scored scenario outside the beam and the protected slot gets status `frozen`. It is not expanded, and aggregation counts it with its path mass and its last outcome distribution ([aggregate.py](../../src/pmfa/forecast/aggregate.py)).

## Consequences

The forecast keeps tail mass, and coverage counts it. The pruning audit can later measure how often the realized outcome ran through a frozen scenario.

## Origin

Checkpoint 4.1; spec, Further Notes, "Changes from the checkpoint documents", item 6. Checkpoint 4.1 kept the top three scenarios at each depth, pruned the rest, and placed no trade when the survivors held under 60% of the probability explored. The 0.6 floor stays as the `low_coverage` exit, but frozen scenarios count toward coverage, so only the mass the Critic left unassigned among siblings lowers it.
