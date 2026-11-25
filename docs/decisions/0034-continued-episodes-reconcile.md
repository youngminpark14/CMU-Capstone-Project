# 0034. A continued episode reconciles and never places an order

After a crash or an exchange error at `execute`, the episode only looks its order up.

## Context

An episode can stop at the `execute` node after its approval: the process dies, or an error escapes, between the approval and the order being recorded. Its checkpoint still holds the approved proposal. Sending it later would act on an approval whose gate ran at an older price, and the human may since have rejected the proposal or let it expire.

## Decision

`run`, `resume` and `expire` all continue such an episode at `execute` and mark it continued ([controller.py](../../src/pmfa/episode/controller.py)). A continued `execute` only reconciles (`_reconcile` in [pipeline.py](../../src/pmfa/episode/pipeline.py)): it looks the order up by its deterministic client order id, in the store and then through `Executor.find_order`, and records what it finds. When no order exists it cancels the proposal and ends the episode `error`, with nothing placed. When the exchange cannot be asked, the episode ends `error` and says the outcome is unknown.

## Consequences

No order is ever sent on a stale approval, and no order is sent twice. An order the exchange accepted before the crash is still found and recorded, so the fill-failure halt counts it. A trade lost to a crash stays lost; the next trigger starts a fresh episode.

## Origin

Build, November 2026.
