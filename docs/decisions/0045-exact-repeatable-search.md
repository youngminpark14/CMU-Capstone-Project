# 0045. Every index search is exact and repeatable

Chroma stores the rows and applies the time filter; the rows that pass it are ranked in code, in a fixed order.

## Context

A backtest answers a rerun from the response cache, which works only if a rerun asks every model the same question. The first two paid backtests did not: the pipeline's scores differed between them, and the second paid again for answers the first had bought.

Two causes were found by running the backtest several times with fixed rules in place of the models. Ledger rows tie on similarity as a rule, because one market forecast at three horizons gives three rows with the same text, and Chroma returned tied rows in an order that changed from run to run: 18 of 24 Critic calls got different calibration examples. And Chroma's nearest-neighbour search is approximate: for the same query on a copy of the same index it returned different passages in 21 of 206 evidence searches, leaving out a nearer passage for a farther one.

## Decision

Each of the three searches reads every row that passes its time filter with `get`, the filter inside the read, and ranks the rows itself ([index.py](../../src/pmfa/knowledge/index.py)): by cosine similarity rounded to six decimals (stored vectors are 32-bit floats), then by a fixed tie-break. Evidence ties go by passage id. Ledger ties go to the later resolution, then the entry id. Events are ordered by their place in their series first ([decision 0044](0044-match-next-event-of-a-series.md)). Chroma's nearest-neighbour `query` is no longer used.

## Consequences

Two runs on the same data ask the same questions, so a rerun costs nothing and gives the same numbers; five full runs with fixed rules, under four hash seeds, gave identical inputs for all 425 model calls. The top of every list is the true nearest, not an approximation. The cost is a read of every eligible row per search, in pages of 10,000 (one read of more than about 32,000 rows fails inside Chroma): about a fifth of a second for the backtest's 700 passages, about one second and 850 MB for 5,000 rows, and about four seconds and 2.8 GB for 20,000. A collection much larger than that needs another design. A model's answers still vary from call to call; the cache makes one run repeatable, not the model.

## Origin

Build, November 2026. The first paid backtests (2026-11-27 and 28) showed that a rerun did not repeat. Checkpoint 3.1 chose a vector database with cosine similarity and top-k retrieval; the top k are kept, computed exactly.
