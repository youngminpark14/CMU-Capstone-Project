# cpi-ladder

**Provenance: hand-made fixture with hand-written model records.** A person wrote every model answer in `cassette.json` (see `scripts/build_examples.py`); none is model output, and nothing here is evidence of forecasting skill.

One forecast, every strike. The August 2026 CPI report triggers an episode on the September 2026 CPI ladder. The critic gives five quantiles of the 12-month CPI change for each of three scenarios; code turns them into a probability for every strike, so the ladder is monotone by construction (T3.2 0.80, T3.3 0.64, T3.4 0.44, T3.5 0.26, T3.6 0.12, T3.7 0.06, T3.8 0.03). The market's own mids at that moment are not monotone (the spreads are wide), and no strike has $500 resting near the touch, so nothing is traded: exit `no_edge`.

- Inputs: seven of the 21 real strikes of `KXCPIYOY-26SEP` (3.2% to 3.8%) and their quotes at 2026-09-11 12:35 UTC; the rest are left out to keep the file small. Quotes are the real Kalshi bid and ask at the as-of time from the snapshot. The snapshot has no depth data, so each order book is one level per side synthesised from one-minute candles (size: a share of the trailing hour's volume), and liquidity is a candle-volume proxy; the simulated fill meets that same book.
- Evidence: the real BLS release (https://www.bls.gov/news.release/archives/cpi_09112026.htm). BLS text is in the public domain; source: U.S. Bureau of Labor Statistics.
- The tree is limited to one level (`forecast.max_depth = 1` in `config_overrides`) to keep the hand-written records short.
- Outcome: the author knew when writing the records that September CPI came in at 3.7 percent; the forecast centres on August's 3.4 percent, as the release suggests, and puts little weight there.
