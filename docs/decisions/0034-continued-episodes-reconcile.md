# 0034. A continued episode reconciles and never places an order

After a crash or an exchange error at `execute`, the episode only looks its order up, and an order whose outcome is unknown waits under a status of its own.

## Context

An episode can stop at the `execute` node after its approval: the process dies, or an error escapes, between the approval and the order being recorded. Its checkpoint still holds the approved proposal. Sending it later would act on an approval whose gate ran at an older price, and the human may since have rejected the proposal or let it expire.

## Decision

`run`, `resume`, `expire` and `reconcile` all continue such an episode at `execute` and mark it continued ([controller.py](../../src/pmfa/episode/controller.py)). A continued `execute` only reconciles (`_reconcile` in [pipeline.py](../../src/pmfa/episode/pipeline.py)): it looks the order up by its deterministic client order id, in the store and then through `Executor.find_order`, and records what it finds. When no order exists it cancels the proposal and ends the episode `error`, with nothing placed. When the exchange cannot be asked, the outcome is unknown. A first run can reach the same state: when the demo executor's order outcome is unknown, or its client order id is already taken, and its lookup by that id fails, `place` raises `ExchangeError` and records no rejection ([demo.py](../../src/pmfa/world/kalshi/demo.py)), and the pipeline looks the order up once more. An unknown outcome is a status, not an exit: the episode and the proposal are saved `outcome_unknown`, nothing more is sent, and the graph waits in `hold`. Each live pass and `pmfa reconcile` resume it into `reconcile`, which makes the same lookup until the exchange answers. The owner asked for the distinct status on 2026-11-27; before, such an episode ended `error` and nothing looked again.

## Consequences

No order is ever sent on a stale approval, and no order is sent twice. An order the exchange accepted before the crash is still found and recorded, so the fill-failure halt counts it. A trade lost to a crash stays lost; the next trigger starts a fresh episode. Two cases are left to a person: an episode that died while sending is not swept up automatically, because a live pass cannot tell a dead process from one still sending (`pmfa report-status` lists it, `pmfa reconcile --episode ID` settles it), and an `outcome_unknown` episode whose checkpoint file is gone stays so.

## Origin

Build, November 2026.
