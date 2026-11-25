# 0028. Backtest quotes, fills and liquidity from candles

The exchange's history has candles only; quotes can be stale, fills are priced on the conservative side, and liquidity is dollars traded.

## Context

Kalshi's history has no order-book depth. One-minute candles exist only for minutes in which something changed.

## Decision

Quotes come from the latest one-minute or hourly candle at or before the clock, so a quiet market's quote can be old ([snapshot/reader.py](../../src/pmfa/world/snapshot/reader.py)). An order arrives at the as-of time plus the configured latency (60 seconds) and meets the one-minute candle containing that moment: buying YES pays that candle's highest YES ask, buying NO pays 1 minus its lowest YES bid, with size capped at 10% of the candle's volume. With no one-minute candle there, the hourly candle is used and the fill is flagged coarse ([snapshot/book.py](../../src/pmfa/world/snapshot/book.py)). The liquidity floor uses dollars traded in the trailing 60 minutes.

## Consequences

A backtest fill is never better than the worst touch in its minute. Reports can separate coarse fills. Liquidity in backtests is a proxy and says so.

## Origin

Build, November 2026.
