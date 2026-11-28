# 0016. What a role may see

Never the price of the market being forecast; evidence only inside delimited blocks; never the Generator's rationale for the Critic.

## Context

A role that sees the price can anchor on it, which defeats the comparison of forecast and price. Evidence text is untrusted and could contain instructions.

## Decision

The event brief carries rules, options or strikes, and the close time, never a price ([outcomes.py](../../src/pmfa/forecast/outcomes.py)); a price-move trigger reaches roles as a sentence with no number ([pipeline.py](../../src/pmfa/episode/pipeline.py)). Passage text and the trigger excerpt are moved out of the input JSON into `<evidence>` blocks whose tag is a hash of their own text ([prompts.py](../../src/pmfa/models/roles/prompts.py)). The Critic's scenario briefs omit the Generator's rationale. The Critic does see up to five resolved ledger forecasts, each with the market price it had, as calibration examples. They are the five most similar to the question, same category first, read in a fixed order: by similarity, then the later resolution, then the entry id ([index.py](../../src/pmfa/knowledge/index.py)).

## Consequences

Agreement between forecast and price means something. A passage cannot close its own block.

## Origin

Build, November 2026. Answers checkpoint 5.1's controlled visibility, under which the Critic sees each scenario's claim and citations but not the Generator's reasoning, and changes checkpoint 2.1, whose reasoning loop fetched the live price before forecasting.
