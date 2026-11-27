# 0031. Shared state goes through the MCP server in process, and directly in backtests

The MCP server is the roles' shared state by default; backtests skip the transport for throughput.

## Context

The specification makes the MCP server the interface to state. Calling it through an in-memory MCP session for every tree node is slow over thousands of backtest episodes.

## Decision

Tree nodes and Critic rejections go through a `StateClient` that calls the MCP tools in process (`state.transport = "mcp"`) or the same service methods directly (`"direct"`) ([client.py](../../src/pmfa/state/client.py)). The backtest world forces `direct` ([world/backtest.py](../../src/pmfa/world/backtest.py)). Episodes, traces, orders and the ledger go to the store directly in both cases.

## Consequences

A test checks that the two flavours return the same records and raise the same errors. Any MCP client can browse the same database.

## Origin

Build, November 2026. Implements the shared MCP state server of checkpoints 4.1 and 5.1 for tree nodes and Critic rejections, and bypasses its transport in backtests.
