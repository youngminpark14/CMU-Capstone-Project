# 0002. The events index holds one document per event

Matching works on events, the unit the forecast is made for.

## Context

The Monitor matches a trigger to the event it bears on. With one index document per market, a ladder event would fill the candidate list with near-identical strikes.

## Decision

The events index stores one document per event: its title, the rules text the markets share, and the strike list (`event_text` in [retriever.py](../../src/pmfa/knowledge/retriever.py)). Code retrieves the top five open events; the Monitor confirms one or none. The full rules are then fetched by ticker.

## Consequences

Five candidates are five different events. The rules shown to the roles are the shared wording, with the strikes listed separately.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 2.
