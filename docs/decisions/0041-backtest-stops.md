# 0041. A backtest applies the same stops as the live modes, and the report says when one tripped

Once a stop trips in a backtest, the rest of that pass sends no order; the report says so.

## Context

Nobody resumes a halt in a backtest, yet a halt shown only as an exit count hides that the trading figures stop there. On the data built on 2026-11-25, [fill_coverage.py](../../scripts/fill_coverage.py) finds that at the trading pass's 55 news triggers, 204 of 4,086 open market sides pass the $500 liquidity floor and 86 of those (42%) had no contracts to fill against. The fill-failure stop trips when more than `oversight.fill_failure_halt_fraction` (0.10) of the last `oversight.fill_failure_window` (10) orders did not fill, once ten exist ([halt.py](../../src/pmfa/execution/halt.py)): two in ten are enough.

## Decision

A backtest applies the same stops as the live modes. After one trips, later episodes still run up to the gate, where the halt is first read ([pipeline.py](../../src/pmfa/episode/pipeline.py)), forecasting and spending model calls; there they end `halted` and send no order. `self_halts` in `metrics.json` names each pass's first halted episode and its reason, and counts the halted episodes from then on ([summary.py](../../src/pmfa/evaluation/summary.py)). The report states each pass's halt under "Exits by reason", and under "Simulated trading after fees" says its trading figures cover the run only up to the halt ([report.py](../../src/pmfa/evaluation/report.py)).

## Consequences

A halt in the scoring pass leaves scores unchanged, because the forecast comes before the gate. With real model forecasts the trading simulation will likely halt itself early on fill failures; this is an expectation, since no agent backtest has run. Whether the fill-failure stop should apply in a backtest is the project owner's call. Until then the default applies it; `oversight.fill_failure_window = 0` turns that one stop off for a run.

## Origin

Build, November 2026.
