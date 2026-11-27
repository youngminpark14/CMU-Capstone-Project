# 0042. Every configuration field has a reader

A setting that nothing reads is removed, and a file that still sets one is refused with a pointer to where the setting lives.

## Context

`triggers.schedule_horizons_days` and `forecast.quantile_levels` were declared, validated, written to `pmfa.toml` and printed by `pmfa config show`, yet nothing read them. The scoring horizons come from `evaluation.horizons_days`, and the five quantile levels are fixed by `QUANTILE_LEVELS` in [quantiles.py](../../src/pmfa/forecast/quantiles.py). An edit to either passed `pmfa config check` and changed nothing.

## Decision

Both fields are removed. `REMOVED_KEYS` in [config.py](../../src/pmfa/config.py) names each one with where its setting lives now. `pmfa config check`, and any load of a configuration file that still sets one, refuses it with that pointer, even at its old default. A recorded configuration, such as a recorded world's, that carries one at its old default still reads, because that value expressed no intent; any other value is refused there too. [test_config_fields.py](../../tests/test_config_fields.py) walks every leaf field of `Config` and fails when its name is never read as an attribute in `src/pmfa` outside `config.py`.

## Consequences

An edit to a removed setting is refused rather than ignored, and a new field whose name nothing reads fails the suite. The guard checks names, not full paths, so it is a floor: an unrelated attribute with the same name satisfies it. A field removed later needs its own entry in `REMOVED_KEYS`, or an old file that sets it is refused as an unknown key with no pointer.

## Origin

Build, November 2026.
