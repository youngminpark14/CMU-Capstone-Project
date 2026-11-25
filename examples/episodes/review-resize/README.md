# review-resize

**Provenance: hand-made fixture with hand-written model records.** A person wrote every model answer in `cassette.json` (see `scripts/build_examples.py`); none is model output, and nothing here is evidence of forecasting skill.

Human review in the loop. The same real inputs as `fed-minutes-hawkish` (the September 2026 FOMC minutes and the October Fed event's quotes) produce an approve-tier proposal, so the episode pauses with a review card (`expected/review_card.json`). The scripted reviewer resizes it to 10 contracts with reason code `size_too_large`; the gate runs again on the smaller order at the current book, and the smaller order executes: buy 10 NO on `KXFEDDECISION-26OCT-H0` at no more than $0.1800 (10 filled).

- Inputs: the real `KXFEDDECISION-26OCT` event and quotes at 2026-10-07 18:05 UTC. Quotes are the real Kalshi bid and ask at the as-of time from the snapshot. The snapshot has no depth data, so each order book is one level per side synthesised from one-minute candles (size: a share of the trailing hour's volume), and liquidity is a candle-volume proxy; the simulated fill meets that same book.
- Evidence: the real minutes (https://www.federalreserve.gov/monetarypolicy/fomcminutes20260916.htm). Federal Reserve text is in the public domain; source: Board of Governors of the Federal Reserve System.
- The review is scripted in `world.json` (`review`): it arrives 90 seconds after the pause, inside the ten-minute expiry.
- Outcome: the author knew the October outcome (a hold) when writing the records.
