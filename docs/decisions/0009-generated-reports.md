# 0009. Dashboards are generated reports

Monitoring is a report built from the state database, not a live service.

## Context

A live dashboard needs a hosted service, which is out of scope for a repository a reviewer clones.

## Decision

The evaluation code computes the tallies a report shows (exits, gate rejections, tiers, cost, calls and latency per episode, the pruning audit) from episode records ([summary.py](../../src/pmfa/evaluation/summary.py)). There is no web server.

## Consequences

Everything a report shows can be recomputed from committed artifacts.

## Origin

Checkpoint 6.1; spec, Further Notes, "Changes from the checkpoint documents", item 9. Checkpoint 6.1 proposed a dashboard tracking drawdown, rejections, fill quantity and calibration drift, "with a cancel-all in one command". Here the report shows the maximum drawdown, gate rejections by reason and a reliability table, and `pmfa halt` sets the halt flag and cancels all open orders.
