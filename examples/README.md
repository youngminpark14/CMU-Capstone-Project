# Sample episodes

Five recorded episodes that replay offline, with no account, no API key and no network:

```bash
uv run pmfa replay examples/episodes/fed-minutes-hawkish     # or just: uv run pmfa replay fed-minutes-hawkish
```

**Every example here is a hand-made fixture with hand-written model records.** No Claude API key
existed where they were built, so every Monitor, Researcher, Generator and Critic answer is fixed
text written in advance (they are in `scripts/build_examples.py`), with an AI coding assistant,
Claude Code, as the rest of this repository was. The examples show the pipeline working from
trigger to decision on real inputs; they are not the output of the pipeline's roles and not
evidence of forecasting skill. The outcome of every real event was known to the author when the
records were written. The replay prints a notice saying so before anything else.

| Example | Exit | What it shows |
| --- | --- | --- |
| [`fed-minutes-hawkish`](episodes/fed-minutes-hawkish/) | `executed` | The spec's 3.1 case: real hawkish FOMC minutes lead to a small NO position, approved by a scripted reviewer. |
| [`phantom-edge-no-trade`](episodes/phantom-edge-no-trade/) | `no_edge` | The 2.1 case: a hot CPI report, but the market had already moved about 22 cents. |
| [`review-resize`](episodes/review-resize/) | `executed` | An approve-tier proposal is resized smaller (`size_too_large`); the gate runs again and the smaller order fills. |
| [`cpi-ladder`](episodes/cpi-ladder/) | `no_edge` | One forecast gives a monotone probability for seven real CPI strikes; no strike meets the liquidity floor. |
| [`single-origin-syndicated`](episodes/single-origin-syndicated/) | `insufficient_origins` | One invented story syndicated twice is one secondary origin, not three. |

For a walkthrough of one example that needs nothing installed, read
[`inputs-and-outputs.md`](inputs-and-outputs.md).

## Layout

Each `episodes/NAME/` directory holds:

| File | Holds |
| --- | --- |
| `world.json` | The inputs: the trigger, the event snapshot (markets, quotes, one-level books), the documents and calendar entries the episode read, the starting state, any scripted human review, and the episode's retrieval calls with their results. |
| `cassette.json` | The model records that replay it: one entry per model call, with the exact input record and the output record. |
| `expected/episode.json` | The outputs: the full trace (thought, action, observation), forecast, proposal, gate, routing, review, orders and exit. |
| `expected/review_card.json` | The card a reviewer saw, when the episode paused for review. |
| `README.md` | What the example shows, where each input came from, and its provenance. |

## Commands

```bash
uv run pmfa replay --list                 # the examples, one line each
uv run pmfa replay NAME --verbose         # also print each step's records
uv run pmfa replay NAME --json            # the episode record as JSON
uv run pmfa replay NAME --check           # compare with expected/; non-zero exit on any difference
uv run python scripts/build_examples.py   # rebuild all five after a code change
```

Replay is strict. A model input that differs from the cassette stops it with `CassetteMiss`, and a
retrieval call that differs from the recorded one stops it with `RecordedKnowledgeMiss`, so a
replay that finishes is the recorded episode, not an approximation. The build script rebuilds from
`data/` when the snapshot and corpus are there and from the committed `world.json` files otherwise.

## Where the inputs came from

- **Exchange data**: Kalshi quotes for the 2026 `KXFEDDECISION` and `KXCPIYOY` events, read from
  the local snapshot (`pmfa data build`) as of each trigger. The snapshot has no depth data, so a
  book is one level per side synthesised from one-minute candles. Exchange data in these files is
  kept small (one event, a handful of markets, one book level per side, no candle series) until
  the exchange's data terms are checked.
- **Evidence**: Federal Reserve minutes, statements and speeches, and BLS CPI releases, from the
  local corpus. Both agencies place their text in the public domain and ask to be credited; each
  example's README names its sources and URLs.
- **Synthetic text**: `single-origin-syndicated` uses an invented story from made-up outlets on
  reserved `.example` domains. No real news text is copied.

Retrieval in a recorded world is replayed from the recorded calls, because `world.json` holds only
the documents the episode touched (plus, for `single-origin-syndicated`, the whole small source
world, which replay indexes again).
