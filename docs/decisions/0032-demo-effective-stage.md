# 0032. Demo mode always runs at the approval stage

Mock funds allow demo orders before the ladder is climbed, and a human approves every one of them.

## Context

The gate refuses real orders below the approval stage, and a fresh state database starts at the backtest stage. The demo exchange's funds are mock, so the ladder, which governs real money, should not block it.

## Decision

In demo mode the gate and routing always use the approval stage, whatever the stored stage is, and the stored stage is not changed (`_stage` in [pipeline.py](../../src/pmfa/episode/pipeline.py)). Routing honours auto only at the autonomous stage, so no demo proposal is routed to auto, and the demo world always pauses approve-tier proposals for a human ([world/live.py](../../src/pmfa/world/live.py)).

## Consequences

Every demo order is approved by a person, even if the stored stage were advanced to autonomous. A demo trade that meets every auto condition is flagged would-be-auto and still goes to review.

## Origin

Build, November 2026.
