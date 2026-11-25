"""The episode controller: one LangGraph pipeline that runs an episode for a trigger.

- ``EpisodeController(world, *, checkpointer=None)`` with ``run(trigger)``,
  ``resume(episode_id, review)``, ``expire(episode_id)`` and ``close()``;
- ``open_checkpointer(path)``: the SQLite checkpointer for pausing in one process
  and resuming in another;
- ``build_graph(world, ...)``: the compiled graph; ``mermaid()``: its wiring as Mermaid text;
- ``episode_id_for(mode, trigger)``: the deterministic episode id (the same in every mode).

Nothing here imports LangGraph or MCP at module load.
"""

from __future__ import annotations

from .controller import EpisodeController, NotPaused, open_checkpointer, state_client
from .graph import build_graph, mermaid
from .pipeline import episode_id_for

__all__ = [
    "EpisodeController",
    "NotPaused",
    "build_graph",
    "episode_id_for",
    "mermaid",
    "open_checkpointer",
    "state_client",
]
