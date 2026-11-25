# 0014. Roles run as single-agent CrewAI kickoffs bound to the gateway

CrewAI defines the roles; the gateway still owns every call, and both runners send the same bytes.

## Context

CrewAI's Anthropic integration does not expose the effort setting, and CrewAI makes extra calls of its own (for example to convert text to JSON) that the gateway could not meter.

## Decision

Each role is a CrewAI agent with no tools, no delegation, no memory and one iteration, run with `Agent.kickoff` ([crewai_runner.py](../../src/pmfa/models/roles/crewai_runner.py)). A custom `BaseLLM` adapter sends CrewAI's messages to the project's backend and returns the validated record, which CrewAI hands back unchanged. The runner raises if a kickoff makes other than exactly one model call. An answer that does not parse aborts the kickoff, and the gateway does the repair retry. The direct runner renders the same system and user text CrewAI would ([prompts.py](../../src/pmfa/models/roles/prompts.py)).

## Consequences

A test asserts the two runners send byte-identical text, so cassettes and cache keys recorded under one work under the other. CrewAI's telemetry, tracing and version check are turned off before it is imported, overriding any inherited value, and its import-time `load_dotenv()` is disabled, so a `.env` file is never read.

## Origin

Build, November 2026.
