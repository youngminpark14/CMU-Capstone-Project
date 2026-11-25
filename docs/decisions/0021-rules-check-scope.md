# 0021. What the rules check can and cannot verify

Code checks the structure of a claim; the Critic judges whether it reads the rules correctly.

## Context

The specification asks for a rules check that a scenario bears on the event as written. Reading rules text is a language task; code can only check what is structural.

## Decision

The rules check ([checks.py](../../src/pmfa/forecast/checks.py)) verifies that, at the resolution level, a scenario asserts outcomes that exist (valid option ids for a categorical event; at least one finite bound, with low below high, for a ladder), and that before the resolution level it asserts no resolution outcome. It does not read the claim's wording against the rules. The Critic can reject a scenario with a reason.

## Consequences

A scenario that names a non-existent option is deleted in code. A scenario with valid outcome ids and a misreading of the rules reaches the Critic.

## Origin

Build, November 2026.
