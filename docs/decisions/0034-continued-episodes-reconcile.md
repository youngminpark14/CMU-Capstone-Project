# 0034. A continued episode reconciles and never places an order

After a crash or an exchange error at `execute`, the episode only looks its order up.

## Context

An episode can stop at the `execute` node after its approval: the process dies, or an error escapes, between the approval and the order being recorded. Its checkpoint still holds the approved proposal. Sending it later would act on an approval whose gate ran at an older price, and the human may since have rejected the proposal or let it expire.

## Decision

`run`, `resume` and `expire` all continue such an episode at `execute` and mark it continued ([controller.py](../../src/pmfa/episode/controller.py)). A continued `execute` only reconciles (`_reconcile` in [pipeline.py](../../src/pmfa/episode/pipeline.py)): it looks the order up by its deterministic client order id, in the store and then through `Executor.find_order`, and records what it finds. When no order exists it cancels the proposal and ends the episode `error`, with nothing placed. When the exchange cannot be asked, the episode ends `error`, says the outcome is unknown, and leaves the proposal as it was, because an order may stand behind it. A first run can end the same way. When the demo executor's order outcome is unknown, or its client order id is already taken, and its lookup by that id fails, `place` raises `ExchangeError` and records no rejection ([demo.py](../../src/pmfa/world/kalshi/demo.py)). The pipeline then looks the order up once more. If it finds the order, it records it and the episode ends by what the exchange did. Otherwise the episode ends `error` with the outcome unknown, and the proposal is not cancelled.

## Consequences

No order is ever sent on a stale approval, and no order is sent twice. An order the exchange accepted before the crash is still found and recorded, so the fill-failure halt counts it. A trade lost to a crash stays lost; the next trigger starts a fresh episode.

## Origin

Build, November 2026.
