# 0026. Retrieval: a hashing embedder by default, and as-of rounding that excludes

Deterministic vectors without a download; an index refuses another embedder; boundary times are rounded toward exclusion.

## Context

Tests and replay need the same vectors in every process with no model download. Chroma stores times as integers, so rounding at the as-of boundary could let a document published a fraction of a second late through.

## Decision

The default embedder hashes word and character n-grams into a fixed-size vector; the local ONNX MiniLM model is optional (`retrieval.embedder = "minilm"`) ([embedders.py](../../src/pmfa/knowledge/embedders.py)). An index records its embedder's identity and refuses to be opened or queried with another. Publish times are rounded up and as-of times down, and the filter runs inside the read from Chroma, before any row is ranked ([index.py](../../src/pmfa/knowledge/index.py); the ranking itself is exact, [decision 0045](0045-exact-repeatable-search.md)). The events index rounds each market's open time up the same way, so a listing row is shown only once every strike it names has opened ([decision 0002](0002-events-index-per-event.md)). Embeddings are always passed explicitly. The vectors rank passages; they do not decide origins, which are resolved by text overlap ([decision 0038](0038-origins-by-text-overlap.md)).

## Consequences

Retrieval is reproducible offline. Paraphrase matching is weaker with the default embedder; the ranking ablation can be run with either.

## Origin

Build, November 2026. Changes checkpoint 3.1's store, Chroma with a small sentence-embedding model, by making a hashing embedder the default and the MiniLM sentence model optional.
