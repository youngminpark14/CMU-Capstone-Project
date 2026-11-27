# single-origin-syndicated

**Provenance: hand-made fixture with hand-written model records.** Every model answer in `cassette.json` is fixed text written in advance, with an AI coding assistant (see `scripts/build_examples.py`); none is the output of the pipeline's roles, and nothing here is evidence of forecasting skill.

The origin rule. A news-triggered trade needs one primary origin or two independent secondary origins. Here one story from a made-up outlet, "Example Wire", is syndicated verbatim by two other made-up outlets; all three are in `world.json`, which (unlike the other examples) is the whole source world, re-indexed on replay. Retrieval merges the three copies into one passage, origin resolution traces it to one secondary origin, and the episode exits `insufficient_origins` before any forecast.

- The news text, the outlets and their publish times are INVENTED for this fixture (the `.example` domains are reserved and resolve nowhere). No real news text is copied.
- The event is the real `KXFEDDECISION-26OCT` with its real quotes at 2026-10-20 14:25 UTC. Quotes are the real Kalshi bid and ask at the as-of time from the snapshot. The snapshot has no depth data, so each order book is one level per side synthesised from one-minute candles (size: a share of the trailing hour's volume), and liquidity is a candle-volume proxy; the simulated fill meets that same book.
- Outcome: the author knew when writing the records that the Committee held in October; the episode stops before any forecast, so the records make no claim about it.
