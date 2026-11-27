# 0015. How the Claude API is called

Effort through output_config, no sampling parameters, closed JSON schemas, and the serving model recorded.

## Context

LangChain's `ChatAnthropic` has an `effort` field, but setting it also adds a thinking parameter. The API rejects sampling parameters for these models, and native structured output needs closed object schemas without numeric or string constraints.

## Decision

The backend ([backend.py](../../src/pmfa/models/backend.py)) passes effort as `output_config.effort` and sends no thinking parameter and no `temperature`, `top_p` or `top_k`. Output uses `with_structured_output(..., method="json_schema")`; every output record forbids extra fields, so every schema object sets `additionalProperties: false`, and ranges are stated in field descriptions and enforced by validators after parsing ([records/roles.py](../../src/pmfa/records/roles.py)). The server-side refusal fallback is on by default (`models.refusal_fallback`) for the models whose documentation lists it, not for Haiku 5.5. The model that actually served each answer is recorded on the call (`CallMeta.served_model`) and priced.

## Consequences

A refusal or a truncated answer is unusable and gets the same single repair retry as a malformed one. Costs follow the model that answered.

## Origin

Build, November 2026. Implements checkpoint 4.1's note that every call gets both a model and an effort level; the default models follow the spec's table, which matches checkpoint 4.1's diagram for the Generator and the Critic (Haiku 5.5; Sonnet 5.5, with Fable at depth 1), not its role table (Sonnet 5.5; Opus 5.5) or checkpoint 5.1's (Opus; Fable).
