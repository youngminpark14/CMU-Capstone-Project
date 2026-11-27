# 0001. Forecast per event, not per market

One scenario tree per event yields consistent probabilities for all of its markets.

## Context

Kalshi events come as sets of markets: a Fed meeting has five, and one CPI event carried 21 strike markets. Forecasting each market separately would cost one search per market and could give strikes on the same ladder probabilities that contradict each other.

## Decision

The forecast engine searches one scenario tree per event. Scenarios carry a distribution over the event's outcome space, and code converts it into a YES probability for every market (`market_probabilities` in [quantiles.py](../../src/pmfa/forecast/quantiles.py)). Sizing then picks the one market and side of the event with the best edge.

## Consequences

Strikes on one ladder are always monotone, because each market's probability is a sum over a superset or subset of the next one's outcomes. One search covers the whole event. At most one order is proposed per event per episode.

## Origin

Checkpoints 3.1 and 4.1; spec, Further Notes, "Changes from the checkpoint documents", item 1. Checkpoint 3.1 mapped a headline to the one market it bears on, and checkpoint 4.1's tree returned "a probability for the market as written", the mass on its YES leaves. Here the tree is searched once per event, and code derives every market's probability from it.
