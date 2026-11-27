"""Embedders: text in, unit vectors out.

Two implementations share one small interface:

- ``HashingEmbedder`` is the default. Hashed word and character n-gram features,
  sublinear term frequency, L2 normalisation, fixed dimension. It needs numpy
  only, downloads nothing, and gives the same vectors in every process, which is
  what tests and replay need.
- ``MiniLMEmbedder`` is the local ONNX ``all-MiniLM-L6-v2`` sentence model that
  ships with chromadb. It is better at paraphrase and is imported lazily.

Every embedder has a name and a version. An index records the ``identity`` of
the embedder that built it and refuses to be opened with another, because
vectors from two embedders are not comparable.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from pmfa.errors import PmfaError


class EmbedderMismatch(PmfaError):
    """An index built by one embedder was opened or queried with another."""


class EmbedderUnavailable(PmfaError):
    """The embedder's model is not present locally and could not be downloaded."""


@runtime_checkable
class Embedder(Protocol):
    """Turns texts into L2-normalised float32 vectors of a fixed dimension."""

    name: str
    version: str
    dimension: int

    @property
    def identity(self) -> str:
        """``name:version:dimension``, recorded on every index the embedder builds."""
        ...

    def embed(self, texts: list[str]) -> np.ndarray:
        """An array of shape ``(len(texts), dimension)`` with unit rows (zero rows for empty text)."""
        ...


def _normalise_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)


_WORD = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")

_STOPWORD_TEXT = """a an and are as at be been but by for from had has have he her his i if in into
is it its of on or our she so than that the their them then there these they this those to was
we were what when which while who will with would you your"""

STOPWORDS = frozenset(_STOPWORD_TEXT.split())
"""Function words carry no topic, so they are left out of the features."""


@lru_cache(maxsize=262_144)
def _bucket(feature: str, dimension: int) -> tuple[int, float]:
    """A stable bucket and sign for a feature: blake2b, never Python's salted ``hash()``."""
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return value % dimension, sign


class HashingEmbedder:
    """Deterministic hashed n-gram features: the default embedder for tests, replay and backtests.

    Word unigrams and bigrams (function words removed) fill one half of the
    vector, character n-grams of the remaining words fill the other. Each half is
    built from sublinear term frequencies (``1 + log tf``) with signed feature
    hashing and normalised on its own, then weighted so the cosine of two
    vectors is ``word_weight`` times the word cosine plus the rest times the
    character cosine. Character n-grams catch inflections ("raise", "raised");
    word features keep unrelated texts that share letters far apart.
    """

    name = "hashing"
    version = "1"

    def __init__(
        self,
        dimension: int = 2048,
        *,
        char_ngrams: tuple[int, ...] = (3, 4, 5),
        word_weight: float = 0.6,
    ) -> None:
        if dimension < 2 or dimension % 2:
            raise ValueError("dimension must be an even number of at least 2")
        if not 0 < word_weight < 1:
            raise ValueError("word_weight must be in (0, 1)")
        self.dimension = dimension
        self._half = dimension // 2
        self._char_ngrams = char_ngrams
        self._word_scale = math.sqrt(word_weight)
        self._char_scale = math.sqrt(1 - word_weight)
        grams = ",".join(str(n) for n in char_ngrams)
        self._identity = f"{self.name}:{self.version}:{dimension}:c{grams}:w{word_weight:g}"

    @property
    def identity(self) -> str:
        return self._identity

    def tokens(self, text: str) -> list[str]:
        """Lower-cased content words: the unit both feature families are built from."""
        return [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS]

    def _features(self, text: str) -> tuple[Counter[str], Counter[str]]:
        words = self.tokens(text)
        word_counts: Counter[str] = Counter(words)
        word_counts.update(f"{a} {b}" for a, b in pairwise(words))
        char_counts: Counter[str] = Counter()
        for word in words:
            padded = f"<{word}>"
            for n in self._char_ngrams:
                char_counts.update(padded[i : i + n] for i in range(len(padded) - n + 1))
        return word_counts, char_counts

    def _half_vector(self, counts: Counter[str], salt: str) -> np.ndarray:
        vector = np.zeros(self._half, dtype=np.float64)
        for feature, tf in counts.items():
            index, sign = _bucket(salt + feature, self._half)
            vector[index] += sign * (1.0 + math.log(tf))
        norm = np.linalg.norm(vector)
        return vector / norm if norm > 0 else vector

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            word_counts, char_counts = self._features(text)
            words = self._half_vector(word_counts, "w:")
            chars = self._half_vector(char_counts, "c:")
            out[row, : self._half] = self._word_scale * words
            out[row, self._half :] = self._char_scale * chars
        return _normalise_rows(out)


MINILM_CACHE_ENV = "PMFA_MINILM_CACHE"
"""Optional directory for the MiniLM model files; chromadb's own cache is used otherwise."""


class MiniLMEmbedder:
    """The local ONNX all-MiniLM-L6-v2 sentence model that ships with chromadb.

    chromadb downloads the model archive on first use into
    ``~/.cache/chroma/onnx_models/all-MiniLM-L6-v2`` (or ``cache_dir``, or
    ``$PMFA_MINILM_CACHE``). With ``allow_download=False`` a missing model is an
    error instead of a download. The model reads at most 256 word pieces per
    text, so passages of 250 words are embedded mostly whole.
    """

    name = "minilm"
    version = "all-MiniLM-L6-v2-onnx"
    dimension = 384
    _MODEL_FILES = (
        "config.json",
        "model.onnx",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "vocab.txt",
    )

    def __init__(self, cache_dir: str | Path | None = None, *, allow_download: bool = True) -> None:
        chosen = cache_dir or os.environ.get(MINILM_CACHE_ENV)
        self._cache_dir = Path(chosen) if chosen else None
        self._allow_download = allow_download
        self._model: Any = None

    @property
    def identity(self) -> str:
        return f"{self.name}:{self.version}:{self.dimension}"

    def model_dir(self) -> Path:
        """Where the model files are, or will be after the download."""
        if self._cache_dir is not None:
            return self._cache_dir / "all-MiniLM-L6-v2"
        return Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"

    def is_present(self) -> bool:
        """True when every model file is already on disk."""
        folder = self.model_dir() / "onnx"
        return all((folder / f).exists() for f in self._MODEL_FILES)

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if not self._allow_download and not self.is_present():
            raise EmbedderUnavailable(
                f"the MiniLM model is not present in {self.model_dir()} and downloads are "
                "disabled; run once with downloads allowed, or use the hashing embedder"
            )
        try:
            from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

            model = ONNXMiniLM_L6_V2()
        except (ImportError, ValueError) as exc:
            raise EmbedderUnavailable(f"the MiniLM embedder cannot load: {exc}") from exc
        model.DOWNLOAD_PATH = self.model_dir()
        try:
            model._download_model_if_not_exists()
        except Exception as exc:
            raise EmbedderUnavailable(
                f"the MiniLM model is not present in {self.model_dir()} and could not be "
                f"downloaded from {model.MODEL_DOWNLOAD_URL}: {exc}"
            ) from exc
        self._model = model
        return model

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        model = self._load()
        vectors = np.asarray(model(list(texts)), dtype=np.float32)
        return _normalise_rows(vectors)


def make_embedder(kind: str) -> Embedder:
    """The embedder ``config.retrieval.embedder`` names."""
    if kind == "hashing":
        return HashingEmbedder()
    if kind == "minilm":
        return MiniLMEmbedder()
    raise ValueError(f"unknown embedder {kind!r}")


__all__ = [
    "MINILM_CACHE_ENV",
    "Embedder",
    "EmbedderMismatch",
    "EmbedderUnavailable",
    "HashingEmbedder",
    "MiniLMEmbedder",
    "make_embedder",
]
