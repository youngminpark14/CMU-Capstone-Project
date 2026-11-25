# 0038. Origins are merged by text overlap, not vector similarity

Two documents are one origin only when one is substantially a copy of the other's text.

## Context

The origin rule counts independent origins, so copies must merge and distinct documents must not. Successive official releases are built on one template and share most of their wording, yet each is its own document with its own numbers. A document vector, or any looser measure of similarity, would merge the August CPI release into the June one.

## Decision

Two documents are one origin when their normalised texts are identical, or when at least `retrieval.origin_copy_overlap` (0.92) of the smaller body's five-word shingles appear in the larger and the smaller has at least half as many shingles ([origins.py](../../src/pmfa/knowledge/origins.py)). A document that quotes another in full, as FOMC minutes quote the statement, is not a copy of it. Documents are resolved in publish-time order, and each takes the origin of the earliest earlier document it copies. Measured on the project's corpus, distinct releases built on one template reach an overlap of 0.89, and a copy with one word in a hundred edited keeps 0.95 ([config.py](../../src/pmfa/config.py)).

## Consequences

Syndicated and lightly edited copies count once; distinct releases, speeches and minutes count separately. A document's origin depends only on documents published no later than itself, so origin resolution reads nothing after the as-of time. A paraphrase that rewrites most sentences counts as a separate origin.

## Origin

Build, November 2026.
