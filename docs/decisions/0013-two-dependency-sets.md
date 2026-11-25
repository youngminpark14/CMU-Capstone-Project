# 0013. Two dependency sets: CrewAI with MCP SDK 1.x, or MCP SDK 2.x without CrewAI

CrewAI and the MCP SDK 2.x line cannot share an environment, so the lockfile carries both sets with a declared conflict.

## Context

The specification asks for the MCP SDK's v2 line and for CrewAI role definitions. CrewAI 1.15.45, the pinned release, requires `mcp>=1.30,<2` and `pydantic<2.13`, so the two cannot be installed together. Checked on PyPI on 2026-11-24: every CrewAI release from 1.7.0 through 1.15.45 pins the MCP SDK to its 1.x line, so no CrewAI release at hand works with MCP SDK 2.x.

## Decision

[pyproject.toml](../../pyproject.toml) declares two dependency groups that conflict: `crewai` (the default, with MCP SDK 1.30) and `mcp2` (MCP SDK 2.3, no CrewAI). Without CrewAI, roles call the gateway directly with the same prompts: the specification's stated fallback (`roles_runner = "auto"` picks whichever is installed). The state server runs on both SDK lines; everything that differs between them is in [mcp_compat.py](../../src/pmfa/state/mcp_compat.py). CI runs the full offline suite in both environments.

## Consequences

`uv sync` gives the default environment; `uv sync --no-group crewai --group mcp2` gives the alternative. The fallback is a tested configuration, not a dead branch. Pydantic stays below 2.13 in both.

## Origin

Build, November 2026.
