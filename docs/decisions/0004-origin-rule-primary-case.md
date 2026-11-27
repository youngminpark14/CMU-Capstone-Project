# 0004. The origin rule gains a primary-source case

An official release alone is enough; secondary sources still need two.

## Context

Checkpoints 2.1, 3.1, 5.1 and 6.1 each required two independent sources before a news-triggered trade. For an official release, such as a BLS CPI release or an FOMC statement, the document is the ground truth itself.

## Decision

A trade needs one primary origin or two independent secondary origins (`origin_rule_met` in [confidence.py](../../src/pmfa/decision/confidence.py)). Primary publishers are configured per series (`primary_sources` in [config.py](../../src/pmfa/config.py)). The rule is checked after research and again in the gate.

## Consequences

Syndicated copies merge into one origin, so one secondary story repeated twice exits `insufficient_origins`. Confidence still rises with the number of independent origins cited, up to three.

## Origin

Checkpoints 2.1, 3.1, 5.1 and 6.1; spec, Further Notes, "Changes from the checkpoint documents", item 4. Checkpoint 2.1 stops when research finds only one source, checkpoint 3.1 says "a single origin means no trade" and caps confidence by the origin count, checkpoint 5.1 skips forecasting with fewer than two, and checkpoint 6.1 requires two for any news-driven trade. None makes an exception for an official release; the primary-source case is the spec's.
