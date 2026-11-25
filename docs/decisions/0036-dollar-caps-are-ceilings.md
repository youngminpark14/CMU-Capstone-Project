# 0036. The dollar caps are ceilings, checked against each call's worst case

A model call that could take a dollar cap past its limit is not sent.

## Context

A call's cost is known only after it returns. A meter that only checks whether a cap is already spent lets the last call before the cap overshoot it by that call's whole cost, and a high-effort call can cost several times the typical one.

## Decision

Before every call, the gateway prices the call's worst case: an upper estimate of its input tokens plus its full `max_tokens` of output on the assigned model ([gateway.py](../../src/pmfa/models/gateway.py)). The meter refuses the call when that worst case would take the episode's or the run's dollar cap past its limit ([meter.py](../../src/pmfa/models/meter.py)). A repair retry is checked the same way. An answer that costs nothing new (cached, from a cassette, or scripted) is served whatever its worst case. When the seconds cap is enforced (the live modes), each call's timeout is the configured request timeout or the seconds the episode has left, whichever is shorter.

## Consequences

Spending stays at or below each dollar cap, as far as the input estimate is an upper bound. A run stops with some budget unspent rather than over it. An episode near its cap ends earlier than it would under an after-the-fact check.

## Origin

Build, November 2026.
