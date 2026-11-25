# 0035. The stops read marked equity; sizing's bankroll stays at cost

The daily loss stop and the drawdown halt count unrealized losses; sizing and the dollar caps share one base at cost.

## Context

Equity at cost (cash plus holdings at what they cost) moves only with fees and settlements. A holding whose price collapses would not count against the daily loss stop or the drawdown halt until its market settled, which is too late for a stop.

## Decision

Equity points are marked: cash plus each open holding at the current best bid on its own side, what it could be sold for now, or at cost when that side has no bid (`marked_equity` in [portfolio.py](../../src/pmfa/execution/portfolio.py)). The paper executor records one at every fill and settlement, the demo executor's `mark` uses the same definition, and the live loop records one at the end of every pass ([live/loop.py](../../src/pmfa/live/loop.py)). The daily loss stop is a fraction of the day's starting equity on that series; the drawdown halt is measured from its peak ([risk.py](../../src/pmfa/decision/risk.py)). The bankroll that sizing uses, and that the gate's dollar caps are fractions of, stays cash plus holdings at cost: the executor's current equity.

## Consequences

Both stops count unrealized losses as well as fees and settled losses. The dollar caps measure exposure at cost, and their base counts holdings the same way, so the gate never rejects at a cap what sizing put exactly at it. One day has one loss-stop level however equity moves during it.

## Origin

Build, November 2026.
