# 0030. Scoring choices: abstentions as the market, intervals by event, the market price

The stricter skill figure counts an abstention as the market price, intervals resample whole events, and the market's price is its YES mid, else its last trade clamped into the standing quote.

## Context

A system that abstains on hard questions can flatter its Brier skill. Markets of one event resolve on the same number and are correlated. Far strikes often have one side of the quote empty, so their mid is undefined, and their last trade can be weeks old and outside the quote that stands.

## Decision

Metrics can drop abstentions or score them as the market price ([metrics.py](../../src/pmfa/evaluation/metrics.py)); the stage benchmark uses the stricter variant ([benchmarks.py](../../src/pmfa/evaluation/benchmarks.py)). Bootstrap intervals resample events with replacement and keep each event's markets together, with a fixed seed; with fewer than two events they report no interval ([bootstrap.py](../../src/pmfa/evaluation/bootstrap.py)). The `market` system's price at an as-of time is the YES mid when both sides are quoted, else the last trade clamped into the standing quote, `[bid or 0, ask or 1]` (`market_price` in [baselines.py](../../src/pmfa/evaluation/baselines.py)). An old trade outside today's one-sided quote is not today's price; the quote is. Reports count how many prices came from each source and how many were clamped.

## Consequences

Abstaining cannot carry the backtest benchmark. Intervals are wide on a small window, and honestly so.

## Origin

Build, November 2026. Fixes the market price behind checkpoint 6.1's forecast-skill measure, the Brier score against the market price at forecast time.
