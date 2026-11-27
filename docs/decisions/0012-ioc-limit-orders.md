# 0012. Orders are immediate-or-cancel limit orders

Nothing rests on the book in v1.

## Context

Resting orders need monitoring, cancellation logic and fill handling over time, and they keep risk open after the episode ends.

## Decision

A proposal's order type and time in force are fixed to `limit` and `immediate_or_cancel` in the record type ([records/decision.py](../../src/pmfa/records/decision.py)); the gate checks both, and the demo payload always sends `immediate_or_cancel` ([demo.py](../../src/pmfa/world/kalshi/demo.py)). The unfilled remainder of an order is gone.

## Consequences

An order's worst case is its own size at its own limit. A partial fill is recorded as such and not chased.

## Origin

Checkpoints 2.1 and 6.1, and the instructors' feedback on checkpoint 2.1; spec, Further Notes, "Changes from the checkpoint documents", item 12. Both checkpoints used limit orders, and checkpoint 2.1 had tools to place and cancel them, without saying how long an order rests. Checkpoint 6.1's rule that a price move before execution "cancels the order and recomputes" is kept: the offers are re-read before sending, and when none is left at the limit the proposal is cancelled and the episode recomputes once or exits `stale_price` ([pipeline.py](../../src/pmfa/episode/pipeline.py)). The feedback on checkpoint 2.1 asked about partially filled orders; here a partial fill is recorded and its remainder dropped.
