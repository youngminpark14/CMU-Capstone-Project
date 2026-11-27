# 0010. Real-money trading is out of scope

The autonomy ladder is kept as stored state and benchmarks, without a real-money path.

## Context

Checkpoints 1.1 and 2.1 start the agent in Kalshi's demo environment with mock funds, with the owner approving trades until its record is good enough for autonomy. Checkpoint 6.1 would also let a smaller order execute automatically on any market at any time. The specification turns this into a ladder of stages (backtest, shadow, approval, autonomous) that governs real-money progression. An educational project on mock funds must not be able to reach a production order.

## Decision

The production adapter has no order methods and cannot sign ([production.py](../../src/pmfa/world/kalshi/production.py)); orders go only to the paper executor or to the demo exchange. The stage ladder and the spec's benchmarks are implemented ([stages.py](../../src/pmfa/execution/stages.py), [benchmarks.py](../../src/pmfa/evaluation/benchmarks.py)) so the record that would justify autonomy can be shown.

## Consequences

Advancing a stage changes routing and the gate's stage check, never where orders go.

## Origin

Checkpoints 1.1, 2.1 and 6.1, and the instructors' feedback on checkpoints 2.1 and 6.1; spec, Further Notes, "Changes from the checkpoint documents", item 10. Checkpoint 6.1 names measures (the Brier score against the market price at forecast time, P&L after fees against a no-trade baseline, the gate rejection rate, trades traced to incomplete episodes, the no-trades, overrides with a reason, and the cost and seconds per episode) but sets no thresholds, and the feedback on checkpoint 2.1 asked for "clearer benchmarks for increasing autonomy". The thresholds (Brier skill above zero on 200 backtested markets; four shadow weeks with positive simulated P&L after fees and drawdown under 10%; 100 approved trades with under 15% overridden) are the spec's, though the spec calls them the checkpoint 6.1 benchmarks. The feedback on checkpoint 6.1 asked under what conditions automatic execution is permitted; here routing honours auto only at the autonomous stage.
