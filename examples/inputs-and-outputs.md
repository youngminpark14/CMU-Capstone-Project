# One episode, inputs to outputs: `fed-minutes-hawkish`

A walkthrough for a reader who installs nothing. Every number below is in the committed files of
[`episodes/fed-minutes-hawkish/`](episodes/fed-minutes-hawkish/); `pmfa replay fed-minutes-hawkish`
prints the same story.

> **Hand-made fixture with hand-written model records.** The inputs are real (Kalshi quotes, FOMC
> minutes), but every model answer is fixed text written in advance, with an AI coding assistant.
> Nothing here is the output of the pipeline's roles or evidence of forecasting skill, and the
> author knew when writing the records that the Committee held in October, so this NO position
> lost.

## The inputs (`world.json`)

- **Trigger.** A new document: the minutes of the September 15-16, 2026 FOMC meeting, published
  2026-10-07 at 18:00 UTC. The episode runs as of 18:05, when the feed noticed it.
- **Event.** `KXFEDDECISION-26OCT`, "Fed decision in Oct 2026?", with its five markets. YES quotes
  at 18:05: hold 0.82 / 0.83, hike 25 bp 0.16 / 0.17, the other three 0.00 / 0.01.
- **Books.** One level per side per market, synthesised from one-minute candles (the snapshot has
  no depth data).
- **Documents.** The minutes, the September 16 statement and a speech by Vice Chair Jefferson: only
  what the episode read. The retrieval calls are recorded with their results, so replay ranks
  passages exactly as the full corpus did.
- **Calendar.** One entry between the as-of time and the close: the September CPI release on
  October 14. It becomes the first level of the scenario tree.
- **Review.** A scripted human approves 60 seconds after the pause.

## The outputs (`expected/episode.json`, `expected/review_card.json`)

1. **Match.** The events index returns one open event; the Monitor confirms it.
2. **Research.** The Researcher writes one query for a further increase and one for holding. Code
   retrieves eight passages published before 18:05; the Researcher keeps four (two from the
   minutes' policy discussion, the statement, the speech) and drops four with reasons. Three
   independent primary origins satisfy the origin rule.
3. **Forecast.** A two-level tree. Level 1, the September CPI report: "still elevated" 0.70,
   "cooling" 0.30. Level 2, the October decision under each: hike 0.33 / hold 0.67 after a hot
   report, hold 0.85 / hike 0.15 after a cool one. Code aggregates the four paths into one
   probability per market: hold 0.708, hike 25 bp 0.277 (market mids 0.825 and 0.165). The
   critic never saw the generator's rationale, and no role saw a price.
4. **Size.** The forecast is shrunk halfway to the market: P(NO on hold) 0.292 becomes 0.233. NO
   costs 0.18, so the edge after the taker fee is 4.3 cents. Quarter-Kelly times confidence 0.67
   (two cited origins of three) is capped at 25 contracts per order: buy 25 NO at no more than
   $0.18, worst case $4.76 including a $0.26 fee, under 0.5% of the $1,000 bankroll.
5. **Gate.** Nineteen checks pass, among them halt, stage, blocklist, origin rule, liquidity floor,
   every cap, limit-order-only and the edge bound ($0.20).
6. **Route.** The category has no calibration record yet, so the trade cannot be automatic: tier
   `approve`. The episode pauses with the review card and expires in ten minutes.
7. **Review.** The scripted reviewer approves at 18:06. The gate runs again at the current book.
8. **Execute.** One immediate-or-cancel limit order: 25 of 25 filled at $0.18 (simulated), fee
   $0.26. Exit `executed`.

`review-resize` is the same episode with a reviewer who resizes the order to 10 contracts.
