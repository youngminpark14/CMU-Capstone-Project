# 0022. Evidence support, the tie rule, and confidence

The exact formulas behind beam order and confidence.

## Context

The specification describes evidence support as a 0-to-1 factor from independent origins and recency, a tie rule within 0.05, and a default confidence formula, without fixing the arithmetic.

## Decision

Evidence support is min(1, sum over cited independent origins of weight x 0.5^(age / half-life) / 2), weight 2 for primary and 1 for secondary ([support.py](../../src/pmfa/forecast/support.py)). Beam order takes, among scenarios within 0.05 of the best remaining path mass x support, the one citing more origins, then fresher evidence, then the higher score, then the lower node id. Confidence is coverage x min(1, origins / 3), halved when the budget ran out, counting only origins cited by the scenarios in the forecast ([aggregate.py](../../src/pmfa/forecast/aggregate.py)).

## Consequences

One fresh primary origin or two fresh secondary ones give full support. Ordering is deterministic. A forecast can retrieve several origins and still have low confidence if its scenarios cite only one.

## Origin

Build, November 2026. Implements checkpoint 4.1's score (probability so far times evidence support), its tie rule (more independent sources, then newer evidence) and its lower confidence when the budget runs out; confidence is capped by the origin count, as in checkpoint 3.1, and does not use checkpoint 4.1's spread of critic scores.
