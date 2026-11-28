# 0008. The depth-1 Critic defaults to Fable 5.2

**Superseded by [0046](0046-models-by-reasoning-needed.md):** the depth-1 Critic and the baselines now default to Opus 5.5.

The current, cheaper listing replaces the Fable 5.1 named in checkpoint 4.1.

## Context

Checkpoint 4.1 named Fable 5.1 for the first-level Critic. Fable 5.2 is the current listing, at $8 and $40 per million input and output tokens against $10 and $50 for 5.1.

## Decision

The `critic_depth1` slot defaults to `claude-fable-5-2` at high effort; deeper levels use `claude-sonnet-5-5` ([config.py](../../src/pmfa/config.py)).

## Consequences

Changing one configuration value reverts it. Every model's listed knowledge cutoff is in the configuration, so the backtest can refuse a window that overlaps it.

## Origin

Checkpoint 4.1; spec, Further Notes, "Changes from the checkpoint documents", item 8. Checkpoint 4.1 names Fable 5.1 at depth 1 in both its role table and its diagram. For deeper levels its table names Opus 5.5 and its diagram Sonnet 5.5; the default here follows the diagram.
