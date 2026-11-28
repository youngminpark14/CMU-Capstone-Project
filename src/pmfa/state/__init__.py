"""State: the SQLite store, the state service, and its MCP server.

- ``pmfa.state.store.SqliteStore``: the ``StateStore`` port over one SQLite file.
- ``pmfa.state.service.StateService``: the operations the MCP server exposes.
- ``pmfa.state.server.build_server``: the MCP server (either SDK line).
- ``pmfa.state.client``: ``connect_in_memory``, ``SyncBridge``, ``StateClient``.

Nothing here imports ``mcp`` at module load.
"""
