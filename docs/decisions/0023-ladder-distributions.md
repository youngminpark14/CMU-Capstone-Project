# 0023. Ladder distributions from five quantiles

A continuous distribution through the Critic's quantiles, exponential tails, and a half-step shift for the reporting increment.

## Context

For a ladder the Critic gives five quantiles of the resolving number. Strikes are compared with a number reported in steps (0.1 for CPI), so "above 3.6%" is a statement about rounding.

## Decision

Code builds a CDF through the 5th, 25th, 50th, 75th and 95th percentiles: linear between them, with exponential tails that carry 5% each and start at the density of the adjacent segment; equal neighbouring quantiles are a point mass ([quantiles.py](../../src/pmfa/forecast/quantiles.py)). A bin (low, high] of reported values gets F(high + d/2) - F(low + d/2) for increment d. Every bin gets at least 1e-9 and the bins are renormalized.

## Consequences

Probabilities are monotone across strikes and never exactly 0 or 1 on a ladder. The tail rule is documented in the code and tested.

## Origin

Build, November 2026.
