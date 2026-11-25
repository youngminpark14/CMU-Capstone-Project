# 0017. Triggers that name their event skip the Monitor

Price moves and schedule ticks already know their event, so no model is asked to match them.

## Context

Matching exists to find which event a new document bears on. A price move or a schedule tick is about one event already.

## Decision

For `price_move` and `schedule` triggers, the match step reads the named event and checks that its series is followed, with no model call ([pipeline.py](../../src/pmfa/episode/pipeline.py)). For a price-move trigger, research only counts passages published after the event's last forecast.

## Consequences

One fewer call per such episode. These episodes cannot re-match after `rules_mismatch`. A price move with no new evidence ends in `no_evidence`.

## Origin

Build, November 2026.
