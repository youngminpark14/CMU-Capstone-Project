# 0024. Tree levels: upcoming scheduled events, then the resolution

Each depth is one scheduled event; the last is always the event's own resolution.

## Context

The tree needs a fixed meaning for each depth so scenarios at one depth are siblings about the same thing.

## Decision

Levels are up to `max_depth - 1` (default 2) of the nearest calendar entries strictly between the as-of time and the event's close, earliest first, then the event's resolution ([levels.py](../../src/pmfa/forecast/levels.py)). With no calendar entries the tree is the resolution level alone. An event closing within 60 minutes gets the resolution level alone on the fast-path slot.

## Consequences

The Generator always knows which event it is making a claim about. The calendar check can verify each level's date.

## Origin

Build, November 2026. Implements checkpoint 4.1's depth limit, under which markets resolve on one to three scheduled events (at most three levels), and its depth-1 flat fallback for a market that closes within the hour.
