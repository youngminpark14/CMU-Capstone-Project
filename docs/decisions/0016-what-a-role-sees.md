# 0016. What a role may see

Never the price of the market being forecast; evidence only inside delimited blocks; never the Generator's rationale for the Critic.

## Context

A role that sees the price can anchor on it, which defeats the comparison of forecast and price. Evidence text is untrusted and could contain instructions.

## Decision

The event brief carries rules, options or strikes, and the close time, never a price ([outcomes.py](../../src/pmfa/forecast/outcomes.py)); a price-move trigger reaches roles as a sentence with no number ([pipeline.py](../../src/pmfa/episode/pipeline.py)). Passage text and the trigger excerpt are moved out of the input JSON into `<evidence>` blocks whose tag is a hash of their own text ([prompts.py](../../src/pmfa/models/roles/prompts.py)). The Critic's scenario briefs omit the Generator's rationale. The Critic does see up to five resolved ledger forecasts, each with the market price it had, as calibration examples.

## Consequences

Agreement between forecast and price means something. A passage cannot close its own block.

## Origin

Build, November 2026.
