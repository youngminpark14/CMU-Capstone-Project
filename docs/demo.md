# Giving a live demo

One command runs one live episode on one Kalshi market, saves every source it read, gives a YES
or NO with a probability, and waits for you to approve or reject its proposal. It runs in a VS Code
terminal (or any terminal) from the repository root. It is shadow mode: live prices read-only, a
fill simulated on paper, nothing sent to any exchange, mock funds only, not trading advice.

## What it shows

Kalshi's Bitcoin series `KXBTCD` ("Bitcoin price Above/below") has a new event every hour, each
with strikes $100 apart: "BTC price on Nov 28 at 1am EST: $59,800 or above?". `pmfa ask KXBTCD`
takes the hourly event that closes next, picks the strike priced nearest even odds, and runs the
whole pipeline on that one market: it reads Coinbase's live Bitcoin price and three crypto news
feeds, the Researcher picks passages, the Generator and the Critic write and score two scenarios
on the fast path, the code compares the forecast with the price after fees, sizes a small order,
checks it against every limit, and stops for your decision. Every step is in the trace.

An hourly price market is close to a coin flip around the current price. The agent is not
expected to beat it; the demo shows the pipeline working on live data, not a trading edge
([decision 0047](decisions/0047-ask-one-market.md)).

## Before you present

- Install once: Python 3.12 and [uv](https://docs.astral.sh/uv/), then `uv sync` in the repository
  root ([README, Setup](../README.md#setup)).
- Run the command once beforehand. The first run reads every feed and the Federal Reserve
  listings and takes the longest; later runs are quicker (the timings are below).
- Mind the clock. An hourly event closes on the hour, and a run needs at least ten minutes of it:
  start between :00 and :50.
- Expect "no trade" often. Most episodes end without an order, by design; the answer and the
  sources are printed either way.

## The commands

Set the Claude API key in the terminal that runs `pmfa`. In bash or zsh:

```bash
export ANTHROPIC_API_KEY="..."
```

In PowerShell (the default VS Code terminal on Windows):

```powershell
$env:ANTHROPIC_API_KEY = "..."
```

A key not scoped to one workspace also needs `ANTHROPIC_WORKSPACE_ID`
([the Claude API key](evaluation.md#the-claude-api-key)). Never paste a key into a file in the
repository. Then:

```bash
uv run pmfa ask KXBTCD                        # one episode; you decide on the terminal
uv run pmfa ask KXBTCD --decide reject        # decide in advance (needed with no terminal)
uv run pmfa ask KXBTCD --market TICKER        # a market of your choice, e.g. another strike
uv run pmfa trace EPISODE_ID                  # the full trace of the episode, step by step
uv run pmfa resolve                           # after the market settles: score and settle
uv run pmfa report-status                     # the paper account, exits and calibration
```

`KXBTCD` is switched off in the default configuration: `pmfa ask` switches it on for its one run
only, so backtests, `pmfa data build`, `pmfa shadow` and `pmfa demo` never follow it.
`--min-minutes N` (default 10) passes over an event closing sooner than that. What it reads is
stored in `data/live-corpus/` with what shadow mode polls, never in the frozen backtest corpus,
and the crypto news and Coinbase prices are retrieved only for the Bitcoin series' episodes.

## What each step prints

1. What it will and will not do: live Kalshi data, read-only and unsigned; live evidence and
   models; a paper fill only; nothing sent to any exchange. Before anything is read it checks
   that the key is set and that shadow mode is not halted.
2. Where the shadow state database is.
3. The question: the event, the market, its rules, when it closes and how many minutes are left.
4. That the episode is focused on that market: two outcomes, YES and NO.
5. The sources gathered: how many documents are visible now, how many passages were indexed, and
   that the price snapshot was read. A feed or the snapshot that cannot be read is one `note:` line.
6. The episode starting, with its trigger id.
7. The episode id (for `pmfa trace`), the sources it kept (title, publisher, time, URL, primary
   or secondary), the agent's answer (YES when its probability of YES is at least 50%, else NO,
   with the probability to a tenth of a percent), the YES and NO prices read after the episode
   (printed for you only: no role saw them), and the proposal in words, with the limit price
   sizing set from the order book, or why there is none.
8. The review card, then the prompt: `approve`, `reject`, or `resize N` (1 to one fewer than
   proposed; another number is asked again). Ctrl-C or Ctrl-D at the prompt rejects: the
   proposal is always decided in the run.
9. What happened: the order and its paper fill, or that nothing was placed.
10. The folder it wrote, and how to see the result once the market has settled.

The folder is `runs/ask/<UTC time>-<market>/` (ignored by git):

- `sources/NN-<publisher>-<title>.md`: one file per kept document, with its URL, publisher,
  time and origin, and the words of the passages the agent used in **bold**;
- `answer.md`: the question, the answer and probability, the prices, the proposal or why there is
  none, your decision, the order and fill, and how to print the full trace;
- `episode.json`: the episode record, every step of the trace included.

## How long it takes and what it costs

Reading the sources took about 45 seconds the first time on a fresh clone and 15 to 20 seconds
after that, in checks on 28 November 2026 (Kalshi, Coinbase, the three feeds and the Federal
Reserve listings, spaced two seconds apart per site). The episode
then makes four short model calls: a query plan and a passage selection (Haiku 5.5), and one
write-and-score pair on the fast path (Sonnet 5.5, low effort). From the listed prices that is
about five cents per run, inside the $2.00 per-episode cap, and the calls should take well
under the 90-second cap of a live episode. Both figures are estimates until a run with a key
measures them; the trace records each call's tokens, cost and time.

## What can go wrong, and what the output then says

| Situation | What it prints |
| --- | --- |
| The current hour closes in under 10 minutes | `not run: the current event ... closes at 06:00 UTC, in 6 minute(s) ... run pmfa ask again after 06:00 UTC ... Or pass --market TICKER`; exit 1, nothing spent. |
| No key set | `not run: no Anthropic API key: set the environment variable ANTHROPIC_API_KEY first`; exit 1, before anything is read. |
| Just after the hour | The new hourly event may not be listed for a few seconds: `... lists the next one shortly after the hour: try again in a minute`; exit 1. |
| Shadow mode is halted | `not run: shadow mode is halted (...) ... pmfa halt --resume --mode shadow clears it`; exit 1, before anything is read. A halt set during the episode (for example a source behind an open paper position that now answers 404) ends it `halted` with the same hint. |
| Kalshi cannot be reached or answers 404 | `not run: ...`; exit 1. |
| No terminal (a script, a pipe) and no `--decide` | `standard input is not a terminal ... Pass --decide approve or --decide reject`; exit 2, before anything is read. |
| A feed or Coinbase cannot be reached | One `note:` line each. Without the price snapshot the agent forecasts from the news alone when it keeps two or more independent news origins (secondary sources need two), and the episode ends `insufficient_origins` when it keeps fewer. A snapshot from an earlier run is never used in its place, so the sources list then shows no `[primary]` line. |
| No strike has both a bid and an ask | `not run: no market of ... has both a bid and an ask right now`; exit 1. |
| The forecast shows no edge after fees | `No trade proposed: no_edge: ...`; the answer is still printed. |
| The favoured side has an edge but too few offers | `No trade proposed: The YES side has the edge ($0.1500 a contract after fees), but its offers are too thin to trade: $400.00 rests near the best price, below the $500.00 floor.` |
| You take longer than 10 minutes to decide | The proposal expires and nothing is placed. |
| The command is killed while it waits (not Ctrl-C or Ctrl-D) | The proposal stays pending until it expires; `pmfa review reject ID --mode shadow` ends it now. `pmfa review approve` cannot approve it: it runs on the default configuration, where `KXBTCD` is off, so the gate refuses it (`followed_series`). |
| The shadow stage was advanced to `autonomous` | `not run: the shadow stage is autonomous ...`: an asked proposal must always wait for you. |

## The offline fallback

With no network or no key, replay a recorded episode in which a reviewer resizes the order and it
fills:

```bash
uv run pmfa replay review-resize
```

## A sample of the output

The numbers below come from the constructed test fixture
([tests/live/fixtures/constructed_ask.json](../tests/live/fixtures/constructed_ask.json)), with
scripted models in place of Claude. **They are made up**: no price here is a real exchange price.

```text
pmfa ask: one shadow-mode episode on one market, now. It reads live Kalshi market data (read-only, unsigned: no key is loaded), live evidence and live models.
A fill is simulated on paper against the live order book; nothing is ever sent to any exchange. Mock funds only; not trading advice.
Any proposal waits for your decision; no answer (Ctrl-C or Ctrl-D at the question) rejects it.
Shadow session open: state in data/state.sqlite.
Event: BTC price on Nov 28, 2026 at 1am EST? (KXBTCD-26NOV2801)
Market: $59,800 or above (KXBTCD-26NOV2801-T59799.99)
Rules: If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) before 1 AM EST is above 59799.99 at 1 AM EST on Nov 28, 2026, then the market resolves to Yes.
Closes: 2026-11-28 06:00 UTC, in 38 minute(s).
Focused on KXBTCD-26NOV2801-T59799.99: two outcomes, YES ($59,800 or above) and NO.
Sources gathered as of 05:22:00 UTC: 2 document(s) visible, 3 passage(s) indexed, price snapshot read.
Running one episode (schedule:KXBTCD-26NOV2801:asked:20261128T052200Z): four short model calls.
Episode episode-d29fe763fcc7e017: pmfa trace episode-d29fe763fcc7e017 prints every step.
Sources kept: 2
  1. [secondary] Bitcoin holds near $59,800 as traders watch the hourly close (cointelegraph.com, published 2026-11-28 04:40 UTC) https://cointelegraph.com/news/constructed-bitcoin-holds
  2. [primary] Bitcoin (BTC-USD) price on Coinbase at 05:22 UTC, November 28, 2026 (coinbase.com, published 2026-11-28 05:22 UTC) https://api.exchange.coinbase.com/products/BTC-USD/ticker
The agent's answer: YES. It puts the chance of YES ($59,800 or above) at 62.6%.
Prices on Kalshi now, read after the episode: YES $0.49, NO $0.53 a contract (the agent never saw them).
Proposal: buy 15 YES contract(s) at $0.49 or less (a limit set from the order book when the proposal was made). The most it can cost is $7.62, including an estimated fee of $0.27; the edge after fees is $0.0454 a contract.
proposal proposal-41c1b9d85e210999 (episode episode-d29fe763fcc7e017)
...
routed to review because (auto conditions that did not hold, or the stage):
  - category_calibrated
...
Approve, reject, or resize to N contracts (fewer than 15)? [approve / reject / resize N]: approve
Your decision: approve
executed: filled: 15 of 15 at <= 0.4900
order paper-order-8aac40007bc4a46d: filled, 15 of 15 YES KXBTCD-26NOV2801-T59799.99
  fill 15 at 0.4900, fee $0.2700 (simulated)
Saved in runs/ask/20261128T052200Z-KXBTCD-26NOV2801-T59799.99: answer.md, episode.json, sources/ (2 file(s)).
After 06:00 UTC, once the market settles: pmfa resolve scores the forecast and settles any paper position, and pmfa report-status shows the account.
```
