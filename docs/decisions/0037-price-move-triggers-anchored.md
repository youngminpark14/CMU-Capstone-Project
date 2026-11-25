# 0037. Price-move triggers are anchored to the event's last trigger

One price step fires one trigger; a second step, a reversal or a sustained trend fires again.

## Context

A price-move trigger fires when a market's YES mid moves 3 cents or more within 30 minutes, read from one-minute candles. Comparing each candle with every price in the trailing window fires again on every later candle while the price from before the move is still inside the window, and again across the edge of the clock bucket a pass reads.

## Decision

Each move is measured from an anchor ([live/triggers.py](../../src/pmfa/live/triggers.py)). At each candle end the mid is compared only with the prices in the window seen since the event's last price-move trigger, the anchor, counting each market's price at the anchor itself. A crossing fires one trigger per event, named by the first ticker that crossed, and moves the anchor to its time. The live loop keeps each event's last trigger in its cursor file, so the chain of crossings is the same from pass to pass.

## Consequences

A single step starts one episode, not one per candle. A second step, a reversal, or a trend that keeps adding up to the threshold starts a new one each time. Scanning the same candles twice yields the same triggers, and the controller returns the stored episode instead of running it again.

## Origin

Build, November 2026.
