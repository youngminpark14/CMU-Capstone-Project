# 0033. Defaults introduced while building

A $1,000 notional bankroll, a $2 cap per episode, and two requests per second to the exchange.

## Context

The specification leaves the notional bankroll unspecified, caps episodes in calls and seconds only, and asks for throttling to the basic rate tier.

## Decision

Backtests and shadow mode use a notional bankroll of $1,000 (`execution.notional_bankroll`). Each episode is also capped at $2.00 of model spend (`budget.max_usd_per_episode`), below the $25 run cap. The Kalshi client sends at most two requests per second (`kalshi.max_requests_per_second`), because the public endpoints answered 429 quickly at higher rates from a shared address. All three are in [config.py](../../src/pmfa/config.py).

## Consequences

A risk unit is $10 by default, so auto routing needs a cost under $10. One expensive episode cannot spend the run's budget. Snapshot builds are slow but polite.

## Origin

Build, November 2026.
