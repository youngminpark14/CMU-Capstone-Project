# 0007. The Critic scores sibling scenarios together

One Critic call per parent keeps sibling probabilities coherent and fits the call budget.

## Context

Scoring each scenario alone lets sibling probabilities sum past 1 and costs one call per scenario.

## Decision

The Critic scores all surviving children of one parent in one call. Code clips each probability to [0, 1] and scales the set down if it sums above 1; the remainder is unexplored mass ([tree.py](../../src/pmfa/forecast/tree.py)).

## Consequences

A full depth-3 tree makes at most 9 Generator and 9 Critic calls, inside the 30-call budget with room for matching, research and evidence follow-ups. Unexplored mass lowers coverage instead of being spread over the named scenarios.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 7. The spec does not name the checkpoint this revises, and the checkpoint documents are not available to this project, so no checkpoint number is given here.
