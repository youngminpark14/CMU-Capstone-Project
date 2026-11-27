# 0029. Source access: manual BLS import, hand-computed fee cases

Where an official source refuses automated clients, the project uses a documented manual path instead of working around it.

## Context

BLS's terms say it may block robots that carry no contact information, and BLS pages answered a generic client with HTTP 403. Kalshi's fee schedule PDF sat behind a browser check that answered 429.

## Decision

The project sends a generic User-Agent with no personal contact details ([config.py](../../src/pmfa/config.py)). BLS text is fetched automatically once per release and otherwise imported from files saved by hand under `data/manual` ([bls.py](../../src/pmfa/world/evidence/bls.py)); release dates come from a committed schedule. The fee tests use hand-computed cases from the published formula and one worked example from the exchange's documentation, each labelled with its source ([fee_schedule_examples.json](../../tests/decision/fee_schedule_examples.json)).

## Consequences

No site check is circumvented. The fee tests pin the formula and the rounding, not numbers copied from the schedule.

## Origin

Build, November 2026.
