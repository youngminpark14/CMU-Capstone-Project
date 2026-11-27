# 0019. How fees enter sizing

Edge math uses the unrounded fee; the proposal's fee is rounded up for the whole order; the edge must survive the rounding.

## Context

The exchange's fee is quadratic in price and rounded up to the cent per order. On small orders the rounding can be a large share of the edge.

## Decision

Choosing the market and side, and finding the highest price that keeps the minimum edge, use the unrounded per-contract taker fee ([sizing.py](../../src/pmfa/decision/sizing.py)). The proposal's fee is the whole order's fee rounded up to the cent ([fees.py](../../src/pmfa/decision/fees.py)), and the edge after that fee must still be at least 2¢, else sizing tries fewer contracts. The gate re-checks the edge with the rounded fee. Fee type and multiplier come from the series; an unknown fee type is refused rather than guessed.

## Consequences

The reported edge is never better than what the order actually pays. A small order whose fee rounds up past the edge is not proposed.

## Origin

Build, November 2026. Implements checkpoint 2.1's sizing calculator, whose fee-adjusted edge "must be exact and repeatable".
