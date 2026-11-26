# 0041. A backtest applies the same stops as the live modes, and the report says when one tripped

Once a stop trips in a backtest, the rest of that pass sends no order; the report says so.

## Context

Nobody resumes a halt in a backtest, and a halt shown only as an exit count hides that the trading figures stop there. Two stops are likely to trip early in a trading pass ([evaluation.md](../evaluation.md#fill-assumptions-and-their-limits)). The fill-failure stop trips when two or more of the last ten orders did not fill ([halt.py](../../src/pmfa/execution/halt.py)), and 42% of the liquid market sides at the trading pass's news triggers had no contracts to fill against ([fill_coverage.py](../../scripts/fill_coverage.py)). The calibration stop trips when the pipeline's calibration error in a category stays above 0.10 for two weeks; the market's own prices on the scored rows of the June 2026 CPI (closed 2026-07-14) have an error of 0.140.

## Decision

A backtest applies the same stops as the live modes, and each pass starts with an equity point at the window's start for them to measure from. After one trips, an episode that reaches the gate ([pipeline.py](../../src/pmfa/episode/pipeline.py)) has already forecast and spent model calls; it ends `halted` there and sends no order. One that ends earlier, such as `no_match` or `no_edge`, never reads the halt. `self_halts` in `metrics.json` names each pass's first halted episode, in run order, and its reason, and counts how many episodes from then on ended halted ([summary.py](../../src/pmfa/evaluation/summary.py)). The report states each pass's halt under "Exits by reason" and says its trading figures cover the run only up to the halt ([report.py](../../src/pmfa/evaluation/report.py)).

## Consequences

A halt in the scoring pass leaves scores unchanged, because the forecast comes before the gate; `pmfa backtest --no-trade` scores without a trading pass. With model forecasts the trading simulation will likely halt itself early, on fill failures or, near the end of July, on calibration if its forecasts of the June CPI are no better calibrated than the market's. These are expectations: no backtest with model forecasts has run. Whether these stops should apply in a backtest is the project owner's call; until then the default applies them, and `oversight.fill_failure_window = 0` turns the fill-failure stop off for a run.

## Origin

Build, November 2026.
