# 0010. Real-money trading is out of scope

The autonomy ladder is kept as stored state and benchmarks, without a real-money path.

## Context

The design has an autonomy ladder that ends in autonomous real-money trading. An educational project on mock funds must not be able to reach a production order.

## Decision

The production adapter has no order methods and cannot sign ([production.py](../../src/pmfa/world/kalshi/production.py)); orders go only to the paper executor or to the demo exchange. The stage ladder and its benchmarks from checkpoint 6.1 are implemented ([stages.py](../../src/pmfa/execution/stages.py)) so the record that would justify autonomy can be shown.

## Consequences

Advancing a stage changes routing and the gate's stage check, never where orders go.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 10; benchmarks from checkpoint 6.1.
