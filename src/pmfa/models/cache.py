"""A content-addressed response cache on disk: one JSON file per key.

The key is a hash of everything that shapes an answer: model, effort,
max_tokens, system text, user text, and the output schema. Role inputs carry
``as_of``, so the as-of time is part of the user text and therefore of the key:
the same question asked at a later time is a different entry, and a backtest
can never be answered from a cache entry made with later knowledge.

A hit never calls the model. Only validated answers are stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from pmfa.records.base import Record


def cache_key(
    *,
    model: str,
    effort: str,
    max_tokens: int,
    system: str,
    user: str,
    output_type: type[Record],
) -> str:
    """The hex SHA-256 of the canonical JSON of the request's answer-shaping parts."""
    material = {
        "model": model,
        "effort": effort,
        "max_tokens": max_tokens,
        "system": system,
        "user": user,
        "schema": output_type.model_json_schema(),
    }
    canonical = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CachedAnswer(BaseModel):
    """One stored answer, with what it cost when it was first made."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    task: str
    model: str
    served_model: str
    effort: str
    output: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    repaired: bool = False


class ResponseCache:
    """Reads and writes ``<directory>/<key>.json``. Writes are atomic."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> CachedAnswer | None:
        path = self._path(key)
        if not path.exists():
            return None
        return CachedAnswer.model_validate_json(path.read_text(encoding="utf-8"))

    def put(self, answer: CachedAnswer) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        text = answer.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self._path(answer.key))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


__all__ = ["CachedAnswer", "ResponseCache", "cache_key"]
