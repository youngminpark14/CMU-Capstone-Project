# phantom-edge-no-trade

**Provenance: hand-made fixture with hand-written model records.** A person wrote every model answer in `cassette.json` (see `scripts/build_examples.py`); none is model output, and nothing here is evidence of forecasting skill.

The spec's 2.1 case: the market already prices the news. The August 2026 CPI report (0.4 percent in the month, 3.4 percent over the year) is hawkish for the September 15-16 Fed decision, and the hand-written forecast says so, but in the five minutes after the release the "Hike 25bps" market moved from about 0.59 to 0.815. The forecast is close to the new price, so no market has the 2-cent minimum edge after fees and shrinkage: exit `no_edge`.

- Inputs: the real `KXFEDDECISION-26SEP` event (all five markets) and quotes at 2026-09-11 12:35 UTC. Quotes are the real Kalshi bid and ask at the as-of time from the snapshot. The snapshot has no depth data, so each order book is one level per side synthesised from one-minute candles (size: a share of the trailing hour's volume), and liquidity is a candle-volume proxy; the simulated fill meets that same book.
- Evidence: the real BLS release (https://www.bls.gov/news.release/archives/cpi_09112026.htm) and the minutes of the July 28-29 FOMC meeting. BLS text is in the public domain; source: U.S. Bureau of Labor Statistics. Federal Reserve text is in the public domain; source: Board of Governors of the Federal Reserve System.
- Outcome: the author knew when writing the records that the Committee raised the rate 25 bp in September; the forecast stays below the market's own hike price, so it claims no foresight.
