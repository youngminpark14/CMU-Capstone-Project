# 0028. Backtest quotes, fills and liquidity from candles; decisions read the as-of book

The exchange's history has candles only. Decisions read the as-of book; only the fill is priced from a later candle, on the conservative side; liquidity is dollars traded.

## Context

Kalshi's history has no order-book depth. One-minute candles exist only for minutes in which something changed.

## Decision

Quotes come from the latest one-minute or hourly candle at or before the clock, so a quiet market's quote can be old ([snapshot/reader.py](../../src/pmfa/world/snapshot/reader.py)). The backtest executor holds two books ([world/backtest.py](../../src/pmfa/world/backtest.py)). Every decision reads the as-of book built from that quote: sizing, the price re-read before sending, the one recompute, the gate (before and after a review), and routing's price move. Only the fill uses a later candle: an order arrives at the as-of time plus the configured latency (60 seconds) and meets the one-minute candle containing that moment: buying YES pays that candle's highest YES ask, buying NO pays 1 minus its lowest YES bid, with size capped at 10% of the candle's volume. With no one-minute candle there, the hourly candle is used and the fill is flagged coarse ([snapshot/book.py](../../src/pmfa/world/snapshot/book.py)). The liquidity floor uses dollars traded in the trailing 60 minutes.

## Consequences

No decision reads a price from after the as-of time, so a later price cannot steer an order. A backtest fill is never better than the worst touch in its minute, and a limit that the later candle does not meet leaves the order unfilled. Reports can separate coarse fills. Liquidity in backtests is a proxy and says so.

## Origin

Build, November 2026.
