# 0025. The pruning audit as posterior mass on frozen scenarios

How much of what happened ran through scenarios the search stopped expanding.

## Context

The specification asks how often the path that happened ran through a frozen scenario. With probabilistic scenarios there is rarely a single path that happened.

## Decision

Once an event resolves, each scenario in the forecast gets weight path mass x its probability of the realized outcome, normalized. The audit reports the share of that posterior on frozen scenarios, and whether the single most likely scenario given the outcome was frozen ([audit.py](../../src/pmfa/forecast/audit.py)).

## Consequences

The audit is a number per episode that can be averaged across a run, rather than a yes-or-no per episode.

## Origin

Build, November 2026.
