# 0032. Demo mode runs at an effective stage of at least approval

Mock funds allow demo orders before the ladder is climbed, and a human still approves them.

## Context

The gate refuses real orders below the approval stage, and a fresh state database starts at the backtest stage. The demo exchange's funds are mock, so the ladder, which governs real money, should not block it.

## Decision

In demo mode the pipeline raises the stage the gate and routing use to approval when the stored stage is below it, without changing the stored stage (`_stage` in [pipeline.py](../../src/pmfa/episode/pipeline.py)). The demo world always pauses approve-tier proposals for a human ([world/live.py](../../src/pmfa/world/live.py)). Auto is still honoured only at the autonomous stage.

## Consequences

Below the autonomous stage, every demo order is approved by a person. A demo trade that meets every auto condition is flagged would-be-auto and still goes to review.

## Origin

Build, November 2026.
