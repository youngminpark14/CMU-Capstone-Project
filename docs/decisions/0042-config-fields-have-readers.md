# 0042. Every configuration field has a reader

A setting that nothing reads is removed, and a file that still sets one is refused as an unknown key.

## Context

`triggers.schedule_horizons_days` and `forecast.quantile_levels` were declared, validated, written to `pmfa.toml` and printed by `pmfa config show`, yet nothing read them. The scoring horizons come from `evaluation.horizons_days`, and the five quantile levels are fixed by `QUANTILE_LEVELS` in [quantiles.py](../../src/pmfa/forecast/quantiles.py). An edit to either passed `pmfa config check` and changed nothing.

## Decision

Both fields are removed. Every section of `Config` in [config.py](../../src/pmfa/config.py) forbids unknown keys, so `pmfa config check`, and any load of a configuration file that still sets one, refuses it as an unknown key. [test_config_fields.py](../../tests/test_config_fields.py) walks every leaf field of `Config` and fails when its name is never read as an attribute in `src/pmfa` outside `config.py`.

## Consequences

An edit to a removed setting is refused rather than ignored, and a new field whose name nothing reads fails the suite. The guard checks names, not full paths, so it is a floor: an unrelated attribute with the same name satisfies it. A published run whose manifest records a field removed later is republished rather than read back.

## Origin

Build, November 2026.
