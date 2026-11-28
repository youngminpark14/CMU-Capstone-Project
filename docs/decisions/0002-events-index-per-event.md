# 0002. The events index holds one document per event, one row per listing change

Matching works on events, the unit the forecast is made for. At any as-of time the index shows one document per event, stored as one row per listing change so that later strikes are not shown early.

## Context

The Monitor matches a trigger to the event it bears on. With one index document per market, a ladder event would fill the candidate list with near-identical strikes.

## Decision

At any as-of time the events index shows one document per event: its title, the rules text the markets share, and the strike list (`event_text` in [retriever.py](../../src/pmfa/knowledge/retriever.py)). A ladder adds strikes after its first markets open, so the event is stored as one row per listing change (`listings`): each distinct market open time starts a row that lists only the markets open by then and is valid until the next one opens or the event closes. The as-of filter inside the Chroma query returns exactly the row current at the as-of time, so a strike never appears before it opens. Code retrieves up to five open events, one per series ([decision 0044](0044-match-next-event-of-a-series.md)); the Monitor confirms one or none. The full rules are then fetched by ticker.

## Consequences

The candidates are different events, at most one per series. The rules shown to the roles are the shared wording, with the strikes listed separately. The strike list in a candidate is the one a trader saw at the as-of time, not the event's final list.

## Origin

Checkpoint 3.1; spec, Further Notes, "Changes from the checkpoint documents", item 2. Checkpoint 3.1's markets collection held "One document per open position on a market so that the title and rules are kept", took the top five by cosine similarity, and had the judge confirm the match. Here a document is an event and the Monitor confirms; the one row per listing change is the build's.
