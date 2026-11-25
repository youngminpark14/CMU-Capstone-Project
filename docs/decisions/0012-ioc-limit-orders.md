# 0012. Orders are immediate-or-cancel limit orders

Nothing rests on the book in v1.

## Context

Resting orders need monitoring, cancellation logic and fill handling over time, and they keep risk open after the episode ends.

## Decision

A proposal's order type and time in force are fixed to `limit` and `immediate_or_cancel` in the record type ([records/decision.py](../../src/pmfa/records/decision.py)); the gate checks both, and the demo payload always sends `immediate_or_cancel` ([demo.py](../../src/pmfa/world/kalshi/demo.py)). The unfilled remainder of an order is gone.

## Consequences

An order's worst case is its own size at its own limit. A partial fill is recorded as such and not chased.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 12. The spec does not name the checkpoint this revises, and the checkpoint documents are not available to this project, so no checkpoint number is given here.
