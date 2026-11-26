# 0040. The snapshot has a content digest

A SHA-256 over the snapshot's data rows alone shows that a rebuilt snapshot holds the data a published run read.

## Context

The snapshot file's own hash (`snapshot_sha256`) differs on every rebuild, because the file also holds the build manifest and its page layout depends on the build. A reviewer who rebuilt the data from the exchange had no way to show it matched a published run.

## Decision

`SnapshotStore.content_sha256` hashes the data tables alone: series, events, markets, candles and coverage, each row keyed by ticker, in a fixed order ([snapshot/store.py](../../src/pmfa/world/snapshot/store.py)). It does not depend on insertion order, internal row ids or file layout. A build clears any recorded digest before it changes a row and records a fresh one when it finishes ([snapshot/build.py](../../src/pmfa/world/snapshot/build.py)), so a build that stops part way leaves none. So does a build made before the digest existed: the data the published run read was built on 2026-11-25 without one, so its snapshot manifest's `content_sha256` is null. A run computes the digest from the rows when none is recorded ([backtest.py](../../src/pmfa/evaluation/backtest.py)). `pmfa data build` prints it ([cli/data.py](../../src/pmfa/cli/data.py)), the snapshot manifest stores it as `content_sha256`, a run manifest as `snapshot_content_sha256` ([manifest.py](../../src/pmfa/evaluation/manifest.py)), and the report's Data line prints it before the file hash ([report.py](../../src/pmfa/evaluation/report.py)).

## Consequences

A rebuild can be matched to a published run by one value. Measured once: the snapshot the [published run](../../evaluation/runs/2026-11-26-market-baseline/manifest.json) read and a from-scratch rebuild made on 2026-11-25 hold the same rows (2 series, 7 events, 104 markets, 840,841 candles, 208 coverage rows) and give the same content digest, `15d471e2b2b6700e7bb137355c241c65df1d67ff0cca68fd2355464682992638`, while their file hashes differ (`a9c3334d...` and `a5155b3f...`). That rebuild took 1,532 seconds, 1,408 of them for the snapshot at two requests per second. The file hash still names the exact file a run read.

## Origin

Build, November 2026.
