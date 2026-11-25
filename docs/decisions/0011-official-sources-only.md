# 0011. Evidence is official sources only in v1

Federal Reserve and BLS releases, plus their release calendar.

## Context

News wires and open-web search widen coverage but bring licensing questions, syndication noise and text that cannot be committed.

## Decision

Evidence comes from Federal Reserve statements, minutes and speeches ([fed.py](../../src/pmfa/world/evidence/fed.py)), BLS CPI and Employment Situation releases ([bls.py](../../src/pmfa/world/evidence/bls.py)), and a release calendar from the FOMC and BLS schedules ([calendar.py](../../src/pmfa/world/evidence/calendar.py)).

## Consequences

Committed text is public domain, with credit to both agencies. The `Evidence` port allows more sources later without changing the pipeline.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 11. The spec does not name the checkpoint this revises, and the checkpoint documents are not available to this project, so no checkpoint number is given here.
