# fed-minutes-hawkish

**Provenance: hand-made fixture with hand-written model records.** A person wrote every model answer in `cassette.json` (see `scripts/build_examples.py`); none is model output, and nothing here is evidence of forecasting skill.

The spec's 3.1 example: hawkish Fed minutes lead to a small NO position. The minutes of the September 15-16, 2026 FOMC meeting ("most participants assessed that another increase in the target range ... would likely be appropriate by year end") trigger an episode on the October 2026 Fed decision. The hand-written forecast puts a 25 bp October hike above the market's price, and the proposal is to buy 25 NO on `KXFEDDECISION-26OCT-H0` at no more than $0.1800 (25 filled): NO on "Fed maintains rate". The trade is approve-tier, so the episode pauses; a scripted reviewer approves it after 60 seconds.

- Inputs: the real `KXFEDDECISION-26OCT` event (all five markets) and quotes at 2026-10-07 18:05 UTC, five minutes after the minutes were published. Quotes are the real Kalshi bid and ask at the as-of time from the snapshot. The snapshot has no depth data, so each order book is one level per side synthesised from one-minute candles (size: a share of the trailing hour's volume), and liquidity is a candle-volume proxy; the simulated fill meets that same book.
- Evidence: the real minutes (https://www.federalreserve.gov/monetarypolicy/fomcminutes20260916.htm), the September 16 statement and a speech by Vice Chair Jefferson. Federal Reserve text is in the public domain; source: Board of Governors of the Federal Reserve System.
- Outcome: the author knew when writing the records that the Committee held in October (this NO position lost); the records follow the minutes, not the outcome.
