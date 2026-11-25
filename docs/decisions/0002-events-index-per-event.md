# 0002. The events index holds one document per event, one row per listing change

Matching works on events, the unit the forecast is made for. At any as-of time the index shows one document per event, stored as one row per listing change so that later strikes are not shown early.

## Context

The Monitor matches a trigger to the event it bears on. With one index document per market, a ladder event would fill the candidate list with near-identical strikes.

## Decision

At any as-of time the events index shows one document per event: its title, the rules text the markets share, and the strike list (`event_text` in [retriever.py](../../src/pmfa/knowledge/retriever.py)). A ladder adds strikes after its first markets open, so the event is stored as one row per listing change (`listings`): each distinct market open time starts a row that lists only the markets open by then and is valid until the next one opens or the event closes. The as-of filter inside the Chroma query returns exactly the row current at the as-of time, so a strike never appears before it opens. Code retrieves the top five open events; the Monitor confirms one or none. The full rules are then fetched by ticker.

## Consequences

Five candidates are five different events. The rules shown to the roles are the shared wording, with the strikes listed separately. The strike list in a candidate is the one a trader saw at the as-of time, not the event's final list.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 2. The spec does not name the checkpoint this revises, and the checkpoint documents are not available to this project, so no checkpoint number is given here.
