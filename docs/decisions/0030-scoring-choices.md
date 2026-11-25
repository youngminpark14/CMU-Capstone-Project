# 0030. Scoring choices: abstentions as the market, intervals by event

The stricter skill figure counts an abstention as the market price, and intervals resample whole events.

## Context

A system that abstains on hard questions can flatter its Brier skill. Markets of one event resolve on the same number and are correlated.

## Decision

Metrics can drop abstentions or score them as the market price ([metrics.py](../../src/pmfa/evaluation/metrics.py)); the stage benchmark uses the stricter variant ([benchmarks.py](../../src/pmfa/evaluation/benchmarks.py)). Bootstrap intervals resample events with replacement and keep each event's markets together, with a fixed seed; with fewer than two events they report no interval ([bootstrap.py](../../src/pmfa/evaluation/bootstrap.py)).

## Consequences

Abstaining cannot carry the backtest benchmark. Intervals are wide on a small window, and honestly so.

## Origin

Build, November 2026.
