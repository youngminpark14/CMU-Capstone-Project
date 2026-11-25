from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from pmfa.knowledge.embedders import (
    EmbedderUnavailable,
    HashingEmbedder,
    MiniLMEmbedder,
    make_embedder,
)
from tests.knowledge.conftest import CPI_RELEASE, FED_SPEECH, FED_STATEMENT, GARDEN

TEXTS = [FED_STATEMENT, "federal funds rate decision", GARDEN]

_DIGEST_SCRIPT = """
import hashlib, sys
from pmfa.knowledge.embedders import HashingEmbedder
texts = sys.stdin.read().split("\\x1e")
vectors = HashingEmbedder().embed(texts)
print(hashlib.sha256(vectors.tobytes()).hexdigest())
"""


def digest(vectors: np.ndarray) -> str:
    return hashlib.sha256(vectors.tobytes()).hexdigest()


def test_hashing_vectors_are_unit_length_and_fixed_size() -> None:
    e = HashingEmbedder()
    v = e.embed([*TEXTS, ""])
    assert v.shape == (4, e.dimension) and v.dtype == np.float32
    assert np.allclose(np.linalg.norm(v[:3], axis=1), 1.0, atol=1e-5)
    assert not v[3].any()


def test_hashing_is_deterministic_across_processes() -> None:
    here = digest(HashingEmbedder().embed(TEXTS))
    assert digest(HashingEmbedder().embed(TEXTS)) == here
    for seed in ("1", "2"):
        out = subprocess.run(
            [sys.executable, "-c", _DIGEST_SCRIPT],
            input="\x1e".join(TEXTS),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert out.stdout.strip() == here


def test_hashing_puts_related_texts_closer_than_unrelated_ones() -> None:
    v = HashingEmbedder().embed([FED_STATEMENT, FED_SPEECH, CPI_RELEASE, GARDEN])
    sims = v @ v.T
    assert sims[0, 1] > sims[0, 3]
    assert sims[1, 2] > sims[1, 3]


def test_identity_names_embedder_version_and_shape() -> None:
    assert HashingEmbedder().identity != HashingEmbedder(512).identity
    assert MiniLMEmbedder().identity == "minilm:all-MiniLM-L6-v2-onnx:384"
    assert isinstance(make_embedder("hashing"), HashingEmbedder)
    assert isinstance(make_embedder("minilm"), MiniLMEmbedder)
    with pytest.raises(ValueError):
        make_embedder("word2vec")


def test_minilm_without_model_and_without_download_is_a_clear_error(tmp_path: Path) -> None:
    e = MiniLMEmbedder(tmp_path / "models", allow_download=False)
    assert not e.is_present()
    with pytest.raises(EmbedderUnavailable, match="not present"):
        e.embed(["federal funds rate"])


def test_minilm_embeds_when_the_model_is_already_cached() -> None:
    e = MiniLMEmbedder(allow_download=False)
    if not e.is_present():
        pytest.skip("the MiniLM model is not cached on this machine")
    v = e.embed(["The Fed held rates steady.", "The FOMC kept its target range.", GARDEN])
    assert v.shape == (3, 384)
    assert float(v[0] @ v[1]) > float(v[0] @ v[2])
