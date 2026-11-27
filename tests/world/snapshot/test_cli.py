"""``pmfa data build`` and ``pmfa data status`` with both builders patched out."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import pmfa.world.evidence.corpus as corpus_module
import pmfa.world.evidence.http as http_module
import pmfa.world.kalshi.production as production_module
import pmfa.world.snapshot.build as build_module
from pmfa.cli.app import app
from pmfa.world.evidence.corpus import CorpusManifest
from pmfa.world.snapshot.store import SnapshotManifest
from tests.world.snapshot.fakes import CPI, FED

runner = CliRunner()


class _Closable:
    def close(self) -> None:
        pass


class _Exchange:
    client = _Closable()


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Point the config at ``tmp_path`` and record what each builder was asked to do."""
    toml = tmp_path / "pmfa.toml"
    toml.write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    monkeypatch.setenv("PMFA_CONFIG", str(toml))
    seen: dict[str, list[dict[str, Any]]] = {"corpus": [], "snapshot": []}

    def fake_corpus(config, start, end, fetcher, *, store=None, now=None):  # type: ignore[no-untyped-def]
        seen["corpus"].append({"start": start, "end": end, "dir": store.directory})
        manifest = CorpusManifest(
            built_at=datetime(2026, 11, 25, tzinfo=UTC),
            failures=["bls.gov: 2 release(s) have no text: CPI-2026-07, CPI-2026-08"],
        )
        return store.write([], manifest=manifest)

    def fake_snapshot(config, exchange, store, *, start, end, minute_days=None, progress=None):  # type: ignore[no-untyped-def]
        series = [s.ticker for s in config.follow.enabled_series()]
        seen["snapshot"].append(
            {
                "start": start,
                "end": end,
                "minute_days": minute_days,
                "series": series,
                "path": store.path,
            }
        )
        if progress:
            progress("KXFEDDECISION: 3 event(s)")
        manifest = SnapshotManifest(
            built_at=datetime(2026, 11, 25, tzinfo=UTC),
            window_start=start,
            window_end=end,
            series=series,
            counts={"events": 3, "markets": 15, "candles_1m": 10, "candles_60m": 5},
            minute_days=minute_days,
        )
        return store.write_manifest(manifest)

    monkeypatch.setattr(corpus_module, "build_corpus", fake_corpus)
    monkeypatch.setattr(build_module, "build_snapshot", fake_snapshot)
    monkeypatch.setattr(production_module, "production_exchange", lambda config, clock: _Exchange())
    monkeypatch.setattr(
        http_module.PoliteFetcher, "from_config", classmethod(lambda cls, config: _Closable())
    )
    return seen


def test_build_runs_both_halves_with_defaults(calls: dict, tmp_path: Path) -> None:
    result = runner.invoke(app, ["data", "build"])
    assert result.exit_code == 0, result.output
    (corpus,) = calls["corpus"]
    (snapshot,) = calls["snapshot"]
    assert corpus["start"] == snapshot["start"] == datetime(2026, 7, 1, tzinfo=UTC)
    assert corpus["dir"] == tmp_path / "data" / "corpus"
    assert snapshot["path"] == tmp_path / "data" / "snapshot" / "snapshot.sqlite"
    assert snapshot["series"] == [FED, CPI]
    assert snapshot["minute_days"] is None
    assert "CPI-2026-07" in result.output  # the manual BLS releases are listed
    assert "snapshot: KXFEDDECISION: 3 event(s)" in result.output
    assert "events 3" in result.output


def test_build_options_reach_the_builders(calls: dict) -> None:
    result = runner.invoke(
        app,
        [
            "data",
            "build",
            "--start",
            "2026-08-01",
            "--end",
            "2026-10-31",
            "--series",
            FED,
            "--minute-days",
            "10",
            "--skip-corpus",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls["corpus"] == []
    (snapshot,) = calls["snapshot"]
    assert snapshot["start"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert snapshot["end"].date().isoformat() == "2026-10-31"
    assert snapshot["series"] == [FED]
    assert snapshot["minute_days"] == 10


def test_skip_snapshot_builds_only_the_corpus(calls: dict) -> None:
    result = runner.invoke(app, ["data", "build", "--skip-snapshot"])
    assert result.exit_code == 0, result.output
    assert len(calls["corpus"]) == 1 and calls["snapshot"] == []


def test_unknown_series_is_refused(calls: dict) -> None:
    result = runner.invoke(app, ["data", "build", "--series", "KXNOPE"])
    assert result.exit_code != 0
    assert calls["snapshot"] == [] and calls["corpus"] == []


def test_status_prints_both_manifests(calls: dict) -> None:
    empty = runner.invoke(app, ["data", "status"])
    assert empty.exit_code == 0, empty.output
    assert empty.output.count("none built") == 2
    assert runner.invoke(app, ["data", "build"]).exit_code == 0
    result = runner.invoke(app, ["data", "status"])
    assert result.exit_code == 0, result.output
    assert '"candles_1m": 10' in result.output
    assert "CPI-2026-08" in result.output
