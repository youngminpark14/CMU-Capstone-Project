# 0020. The gate recomputes every bound and never returns an order

An independent re-derivation, after the self-halt conditions.

## Context

A gate that reuses sizing code would repeat any bug in it. A gate that could adjust an order would be a second sizing step with no check after it.

## Decision

The gate ([gate.py](../../src/pmfa/decision/gate.py)) recomputes the shrunk probability, the fee, the edge-preserving bound (in closed form, where sizing searches the ticks) and every exposure from its own inputs. It returns `GateResult`, which has no order field; the proposal is a frozen record. The gate node first runs the self-halt conditions ([halt.py](../../src/pmfa/execution/halt.py)), so a tripped stop sets the persisted flag and cancels open orders before the gate looks at the proposal.

## Consequences

A property test checks that the gate approves what sizing proposes, from different arithmetic. A rejected proposal ends the episode; it is never retried.

## Origin

Build, November 2026. Follows checkpoints 5.1 and 6.1, where a rejection from the Risk Manager gate ends the episode, rather than checkpoint 2.1's option to resize after a rejection.
