# 0003. Roles are record-only

Code executes every tool call; a role only reads a record and returns one.

## Context

The checkpoints had agents calling tools. With evidence text in the prompt, a tool-calling agent gives an injected passage a path to an action.

## Decision

Each role receives one input record and returns one output record ([records/roles.py](../../src/pmfa/records/roles.py)). Code runs every retrieval, check and order. The Critic's request for more evidence is a field in its output (`ScenarioScore.evidence_request`); code decides whether to run it.

## Consequences

Evidence text cannot trigger an action; at most it can distort a probability, which the caps bound. Roles can be replaced by scripted records in tests, and every role call is one gateway call that can be metered, cached and recorded.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 3. The spec says only that "the checkpoints had agents calling tools"; it does not name the checkpoint, and the checkpoint documents are not available to this project, so no checkpoint number is given here.
