"""The episode graph: one LangGraph ``StateGraph`` that every run mode shares.

``build_graph(world, ...)`` closes over the world's ports, so nothing but plain
JSON goes into graph state and nothing but the thread id into the run config.
The beam search runs as its own nodes (``forecast_start``, then
``forecast_step`` once per tree level, then ``forecast_finish``), so the
checkpointer saves the search between levels. ``pause`` only calls
``interrupt()``; ``review`` applies the human's answer after the resume; and
``execute`` is separate from both, so resuming never repeats a side effect.
The same holds for an order whose outcome is unknown: ``hold`` only calls
``interrupt()``, and ``reconcile`` looks the order up after the resume and never sends.
``mermaid()`` draws the same wiring for the docs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pmfa.ports import World
from pmfa.state.client import StateClient

from .pipeline import EpisodeState, Pipeline
from .trace import Tracer

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

END = "end"

EDGES: dict[str, tuple[str, ...]] = {
    "trigger": ("match",),
    "match": ("research", "record"),
    "research": ("forecast_start", "record"),
    "forecast_start": ("forecast_step", "record"),
    "forecast_step": ("forecast_step", "forecast_finish", "record"),
    "forecast_finish": ("size", "match", "record"),
    "size": ("gate", "record"),
    "gate": ("route", "execute", "record"),
    "route": ("execute", "pause", "record"),
    "pause": ("review",),
    "review": ("gate", "record"),
    "execute": ("size", "hold", "record"),
    "hold": ("reconcile",),
    "reconcile": ("hold", "record"),
    "record": (END,),
}
"""Where each node can go next. ``record`` runs last on every path that ends the episode;
``hold`` is where an episode whose order outcome is unknown waits."""

RECURSION_LIMIT = 200
"""Steps one invocation may take: a re-match and a recompute stay far below it."""


def _next(state: EpisodeState) -> str:
    return state["goto"]


def _wire(nodes: Mapping[str, Callable[[EpisodeState], dict[str, Any]]]) -> Any:
    from langgraph.graph import END as LG_END
    from langgraph.graph import START, StateGraph

    builder = StateGraph(EpisodeState)
    for name in EDGES:
        builder.add_node(name, nodes[name])
    builder.add_edge(START, "trigger")
    for name, targets in EDGES.items():
        mapped = {t: (LG_END if t == END else t) for t in targets}
        if len(targets) == 1:
            builder.add_edge(name, next(iter(mapped.values())))
        else:
            builder.add_conditional_edges(name, _next, mapped)
    return builder


def build_graph(
    world: World,
    *,
    client: StateClient,
    checkpointer: BaseCheckpointSaver[Any],
    tracer: Tracer | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """The compiled episode graph over ``world``. One episode is one thread (its episode id)."""
    pipeline = Pipeline(world, client, tracer or Tracer(world.store, world.clock))
    return _wire(pipeline.nodes()).compile(checkpointer=checkpointer)


def mermaid() -> str:
    """The episode graph as Mermaid text, for the docs. Needs no world: nodes are not run."""

    def placeholder(state: EpisodeState) -> dict[str, Any]:  # pragma: no cover - never run
        raise AssertionError("the Mermaid graph is drawn, not run")

    return _wire(dict.fromkeys(EDGES, placeholder)).compile().get_graph().draw_mermaid()


__all__ = ["EDGES", "build_graph", "mermaid"]
