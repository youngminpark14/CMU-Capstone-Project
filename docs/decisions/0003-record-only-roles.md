# 0003. Roles are record-only

Code executes every tool call; a role only reads a record and returns one.

## Context

In the checkpoints, the models acted for themselves: in checkpoint 2.1 the reasoning loop's actions call tools for market data, search, sizing, the risk gate and orders; in checkpoint 4.1 the Generator and the Critic, like the controller, read and write the tree-state server through tools; and in checkpoint 5.1 the Critic asks the Researcher for evidence directly. With evidence text in the prompt, a tool-calling agent gives an injected passage a path to an action.

## Decision

Each role receives one input record and returns one output record ([records/roles.py](../../src/pmfa/records/roles.py)). Code runs every retrieval, check and order. The Critic's request for more evidence is a field in its output (`ScenarioScore.evidence_request`); code decides whether to run it.

## Consequences

Evidence text cannot trigger an action; at most it can distort a probability, which the caps bound. Roles can be replaced by scripted records in tests, and every role call is one gateway call that can be metered, cached and recorded.

## Origin

Checkpoints 2.1, 4.1 and 5.1; spec, Further Notes, "Changes from the checkpoint documents", item 3. Checkpoint 5.1's rule that "Agents never message each other in free text" is kept: roles exchange structured records, and the controller decides which records each one reads.
