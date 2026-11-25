# 0004. The origin rule gains a primary-source case

An official release alone is enough; secondary sources still need two.

## Context

The checkpoints required two independent sources before a news-triggered trade. For an official release, such as a BLS CPI release or an FOMC statement, the document is the ground truth itself.

## Decision

A trade needs one primary origin or two independent secondary origins (`origin_rule_met` in [confidence.py](../../src/pmfa/decision/confidence.py)). Primary publishers are configured per series (`primary_sources` in [config.py](../../src/pmfa/config.py)). The rule is checked after research and again in the gate.

## Consequences

Syndicated copies merge into one origin, so one secondary story repeated twice exits `insufficient_origins`. Confidence still rises with the number of independent origins cited, up to three.

## Origin

Spec, Further Notes, "Changes from the checkpoint documents", item 4.
