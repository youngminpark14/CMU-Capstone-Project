# 0025. The pruning audit as posterior mass on frozen scenarios

How much of what happened ran through scenarios the search stopped expanding.

## Context

The specification asks how often the path that happened ran through a frozen scenario. With probabilistic scenarios there is rarely a single path that happened.

## Decision

Once an event resolves, each scenario in the forecast gets weight path mass x its probability of the realized outcome, normalized. The audit reports the share of that posterior on frozen scenarios, and whether the single most likely scenario given the outcome was frozen ([audit.py](../../src/pmfa/forecast/audit.py)).

## Consequences

The audit is a number per episode that can be averaged across a run, rather than a yes-or-no per episode.

## Origin

Build, November 2026. Measures what checkpoint 4.1's risk section checks at resolution, whether the realized path ran through a pruned branch, but raises no floor in response, since the build has no probability floor to raise ([decision 0005](0005-protected-slot-only-tail-rule.md)).
