"""Knowledge: chunking, embedders, the three Chroma indexes, origin resolution, ranking.

``ChromaKnowledge`` implements the ``Knowledge`` port. Names are loaded on first
use, so importing this package pulls in neither numpy nor chromadb.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pmfa.knowledge.embedders import (
        Embedder,
        EmbedderMismatch,
        EmbedderUnavailable,
        HashingEmbedder,
        MiniLMEmbedder,
        make_embedder,
    )
    from pmfa.knowledge.retriever import ChromaKnowledge

_EXPORTS = {
    "ChromaKnowledge": "pmfa.knowledge.retriever",
    "Embedder": "pmfa.knowledge.embedders",
    "EmbedderMismatch": "pmfa.knowledge.embedders",
    "EmbedderUnavailable": "pmfa.knowledge.embedders",
    "HashingEmbedder": "pmfa.knowledge.embedders",
    "MiniLMEmbedder": "pmfa.knowledge.embedders",
    "make_embedder": "pmfa.knowledge.embedders",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'pmfa.knowledge' has no attribute {name!r}")
    return getattr(import_module(module), name)


__all__ = [
    "ChromaKnowledge",
    "Embedder",
    "EmbedderMismatch",
    "EmbedderUnavailable",
    "HashingEmbedder",
    "MiniLMEmbedder",
    "make_embedder",
]
