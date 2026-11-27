# 0043. A saved trace's starting state is complete and as of the clock

A recording keeps what the episode's decisions read from the store as of the clock, and nothing dated later.

## Context

A recording used to take every stored fill, whatever its time, and no settlements, unfilled orders, equity points or critic rejections. Since a backtest fill is timed at the end of its candle ([decision 0039](0039-order-outcome-time.md)), the second of two scoring episodes at one clock time saved a trace holding the first one's fill, priced after its own as-of time. A trace saved after a settlement, an unfilled order, a fall in equity or a critic rejection could also replay to a different decision ([decision 0027](0027-record-and-replay.md)), because the rebuilt world's account, stops or Generator input (the claims already rejected) differed.

## Decision

When a recording starts ([recording.py](../../src/pmfa/world/recording.py)), it reads from the store, as of the clock's time: the prior orders whose outcome was known, in the order sent, each with its fills up to then (`orders`); the settlements; the equity points; the resolved ledger entries; the stage and the halt flag. For each event the episode reads, it takes the prior critic rejections and the time of the last forecast (`forecast_times`) when the episode first touches that event. `build_recorded_world` ([recorded.py](../../src/pmfa/world/recorded.py)) seeds a store with them, writes a first equity point at the trigger time only when the file gives none, and refuses one order id given both in `orders` and in `fills`, which hand-made fixtures may still use for holdings.

## Consequences

A world rebuilt from the file answers `balance()`, `positions()`, the risk state and the daily loss, drawdown, fill-failure and calibration stops as the recorded world did, and its starting state holds nothing dated after its as-of time ([test_round_trip.py](../../tests/world/recorded/test_round_trip.py), [test_trace_same_instant.py](../../tests/evaluation/backtest/test_trace_same_instant.py)). Only the books the episode's own orders filled against (`fill_books`) are priced later. The five committed examples are unchanged. Not carried: the prior episodes' cited documents and their remembered content hashes, which only the source-change stops read. In a backtest's frozen corpus no document changes, so those stops cannot fire where traces are saved, nor in a rebuilt world.

## Origin

Build, November 2026.
