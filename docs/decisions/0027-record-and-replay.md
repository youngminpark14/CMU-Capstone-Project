# 0027. Record and replay with ordered cassettes

A cassette is the ordered list of an episode's model calls, checked for equal inputs on replay.

## Context

Replay must reproduce an episode exactly without a key, and must fail loudly if the pipeline asks something different from what was recorded.

## Decision

A cassette is one JSON file per episode with every call in order: task, slot, model, effort, input record, output record, usage and latency ([cassette.py](../../src/pmfa/models/cassette.py)). Calls that ended without an answer (a cap, or two failed validations) are recorded too. On replay each call must ask for the recorded task with an equal input record, or replay stops and names the first difference. Each entry records where its answer came from: `live`, `cache` or `scripted`.

## Consequences

A replayed episode reaches the same exit. An answer that was written by hand rather than produced by a model is labelled `scripted`, not passed off as live.

## Origin

Build, November 2026.
