# 0039. A backtest order's outcome has a time

A paper fill priced from a later candle counts only from that candle's end.

## Context

A backtest fill is priced from a candle after the as-of time ([decision 0028](0028-backtest-data-and-fills.md)) but was stamped at the as-of time, so a second episode at the same clock time could size and gate on it. Two documents in the frozen corpus share a publication instant (2026-09-18 13:30 UTC), so the case was reachable. No published number was affected: the only published run is market-only and placed no order.

## Decision

The fill book is dated at the end of the candle that priced it, else an hour after the arrival time (the end of the hour searched), or the arrival time when the market had closed ([snapshot/book.py](../../src/pmfa/world/snapshot/book.py)). The paper executor stamps the fills with the later of that date and the clock's time ([paper.py](../../src/pmfa/execution/paper.py)), and the order carries it as `OrderResult.outcome_at` (optional; set only when later than `placed_at`). The account is read as of the clock and each equity point as of its own time, and the fill-failure and source-change stops count an order only from its outcome time ([halt.py](../../src/pmfa/execution/halt.py)). The trading pass starts each episode at the latest of its trigger's time, the clock's time and the latest stored order outcome ([backtest.py](../../src/pmfa/evaluation/backtest.py)); the scoring pass keeps its fixed as-of times and relies on the time filter. The final settlement of a run waits for the last order's outcome.

Holding back a pending order's worst case in the account was not taken: it takes more code and makes the simulation trade less than the agent would.

## Consequences

No decision made before an order's outcome time sees its price or outcome. An episode that starts after its trigger takes its start time as its as-of time; `metrics.json` counts these as `late_starts`, and the report states them.

## Origin

Build, November 2026.
