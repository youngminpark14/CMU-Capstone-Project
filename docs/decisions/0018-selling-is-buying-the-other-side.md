# 0018. Selling is buying the other side

Every order buys YES or NO; the exchange nets opposite holdings.

## Context

Positions must be reduced when a re-forecast turns against them, but a separate sell path doubles the order logic and its checks.

## Decision

A proposal always buys one side ([records/decision.py](../../src/pmfa/records/decision.py)). Sizing computes a target on the best side and proposes the difference from the holding; if the best side is opposite the holding, the order covers the holding and then the target ([sizing.py](../../src/pmfa/decision/sizing.py)). On the demo exchange, buying NO at p is an ask on the YES leg at 1 - p. The paper portfolio closes opposite pairs for $1 each. A holding whose own side is still the best edge, at or above its target, is left alone: there is no trim toward a smaller target.

## Consequences

One order path covers buying and selling. Holdings only shrink when the other side has the better edge.

## Origin

Build, November 2026. Implements checkpoint 1.1's step 7 (place an order, reduce, exit, or do nothing) and its rule of recomputing a position and "trading the difference"; a holding is reduced only when the other side has the better edge.
