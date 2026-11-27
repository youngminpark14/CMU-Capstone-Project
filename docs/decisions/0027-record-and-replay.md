# 0027. Record and replay with ordered cassettes

A cassette is the ordered list of an episode's model calls, checked for equal inputs on replay.

## Context

Replay must reproduce an episode exactly without a key, and must fail loudly if the pipeline asks something different from what was recorded.

## Decision

A cassette is one JSON file per episode with every call in order: task, slot, model, effort, input record, output record, usage and latency ([cassette.py](../../src/pmfa/models/cassette.py)). Calls that ended without an answer (a cap, or two failed validations) are recorded too. On replay each call must ask for the recorded task with an equal input record, or replay stops and names the first difference. Each entry records where its answer came from: `live`, `cache` or `scripted`. The cassette replays against a recorded world, one file of what else the episode read, including the store's starting state as of the clock (what it leaves out is in [decision 0043](0043-trace-starting-state.md)).

## Consequences

A replayed episode reaches the same exit, given that recorded world. An answer that was written by hand rather than produced by a model is labelled `scripted`, not passed off as live.

## Origin

Build, November 2026. Implements checkpoint 6.1's aim that any trade can be replayed, from a cassette and a recorded world rather than from the shared state alone.
