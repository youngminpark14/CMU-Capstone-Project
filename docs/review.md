# Review in ten minutes

A path through the repository for a reviewer. Nothing to install. Terms are defined in the
[glossary](glossary.md).

1. **One sample episode.** Open [examples/inputs-and-outputs.md](../examples/inputs-and-outputs.md),
   then [examples/episodes/fed-minutes-hawkish/](../examples/episodes/fed-minutes-hawkish/): its
   README, `world.json` (inputs) and `expected/episode.json` (outputs). Look for: the trace as
   thought, action and observation steps, where thoughts are the only lines a model wrote; the
   passage ids each scenario cites, with source, URL and publish time; and the notice that the model
   records are hand-written.
2. **The guardrail table.** [guardrails.md](guardrails.md) lists every guardrail, whether
   code, the exchange or a human enforces it, and the test that shows it.
3. **The sixteen scenario tests.** [tests/episodes/test_scenarios.py](../tests/episodes/test_scenarios.py):
   `test_01` to `test_16` run whole episodes against fixed worlds and assert what was decided (exit,
   forecast, proposal, routing tier, orders), for example that a halted agent sends no order in any
   mode and that a canary document dated after the as-of time is never retrieved.
4. **The evaluation report.** [evaluation/runs/2026-11-26-market-baseline/report.md](../evaluation/runs/2026-11-26-market-baseline/report.md).
   Look for: the table of which systems ran and which did not, sample sizes by event as well as by
   market, and the section on how look-ahead was prevented.
5. **The decision records.** [decisions/README.md](decisions/README.md): 44 short
   records, each with its reasons and where it came from: the capstone checkpoints it changes or
   answers, the item of the specification, or the build.
6. **The checkpoints, traced.** [checkpoints.md](checkpoints.md) takes each of the six
   capstone checkpoints, and each point of the instructors' feedback, to the file and test that
   carry it. Look for: what was kept, changed or dropped and why, how each piece of feedback was
   answered, and the list of proposals not done here.
