"""The snapshot's content digest: the same for the same rows, different for any changed row."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.world.snapshot.build import build_snapshot
from pmfa.world.snapshot.store import INTERVALS, SnapshotStore
from tests.world.snapshot.fakes import AUG, BUILT, JUL, A, FakeExchange, Interrupted, utc


def _copy_rows_in_reverse(source: SnapshotStore, path: Path) -> SnapshotStore:
    """A second store holding the same rows, written in the opposite order."""
    copy = SnapshotStore(path)
    for ticker in reversed(source.series_tickers()):
        copy.put_series(source.get_series(ticker))
    events = [source.get_event(t) for t in source.event_tickers()]
    for event in reversed(events):
        copy.put_event(event.model_copy(update={"markets": list(reversed(event.markets))}))
    tickers = [m.ticker for e in events for m in e.markets]
    lo, hi = utc(2000, 1, 1), utc(2100, 1, 1)
    for ticker in reversed(tickers):
        for interval in reversed(INTERVALS):
            span = source.coverage(ticker, interval)
            if span is None:
                continue
            rows = source.candles(ticker, interval, after=lo, until=hi)
            copy.put_candles(ticker, interval, reversed(rows), start=span[0], end=span[1])
    return copy


def test_the_same_rows_in_another_order_and_build_give_the_same_digest(
    store: SnapshotStore, tmp_path: Path
) -> None:
    copy = _copy_rows_in_reverse(store, tmp_path / "copy.sqlite")
    manifest = store.manifest()
    assert manifest is not None
    copy.write_manifest(
        manifest.model_copy(update={"built_at": BUILT + timedelta(days=1), "elapsed_seconds": 99.0})
    )
    ids = "SELECT ticker, id FROM markets ORDER BY ticker"
    assert copy._query(ids) != store._query(ids)  # the surrogate ids differ
    assert copy.manifest() != store.manifest()
    assert copy.content_sha256() == store.content_sha256()
    copy.close()


def test_a_build_stores_its_content_digest_in_the_manifest(config: Config, tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "built.sqlite")
    manifest = build_snapshot(config, FakeExchange(), store, end=BUILT)
    assert manifest.content_sha256 is not None and len(manifest.content_sha256) == 64
    assert manifest.content_sha256 == store.content_sha256()
    assert store.manifest() == manifest
    store.close()


def test_an_interrupted_rebuild_leaves_no_stale_digest(config: Config, tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "rebuilt.sqlite")
    first = build_snapshot(config, FakeExchange(), store, end=utc(2026, 8, 1))
    assert store.event_tickers() == [JUL] and first.content_sha256 == store.content_sha256()

    # A later rebuild stores the August event, then stops at its first candle request.
    with pytest.raises(Interrupted):
        build_snapshot(config, FakeExchange(fail_after=0), store, end=BUILT)
    assert store.event_tickers() == [JUL, AUG]
    assert store.content_sha256() != first.content_sha256
    left = store.manifest()
    assert left is not None and left.content_sha256 is None  # no claim about rows it never saw
    assert left.model_copy(update={"content_sha256": first.content_sha256}) == first

    finished = build_snapshot(config, FakeExchange(), store, end=BUILT)
    assert finished.content_sha256 == store.content_sha256() != first.content_sha256
    store.close()


_MARKET_A = f"(SELECT id FROM markets WHERE ticker = '{A}')"
_ONE_NULL_CANDLE = (
    "(SELECT market_id, interval, end_ts FROM candles WHERE open_interest IS NULL "
    "ORDER BY market_id, interval, end_ts LIMIT 1)"
)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            f"UPDATE candles SET bid_c = bid_c + 1 WHERE market_id = {_MARKET_A} "
            "AND interval = 1 AND end_ts = (SELECT MIN(end_ts) FROM candles "
            f"WHERE market_id = {_MARKET_A} AND interval = 1)",
            id="a candle value",
        ),
        pytest.param(f"UPDATE markets SET result = 'no' WHERE ticker = '{A}'", id="a result"),
        pytest.param(
            f"UPDATE coverage SET end_ts = end_ts + 60 WHERE market_id = {_MARKET_A} "
            "AND interval = 1",
            id="a coverage row",
        ),
        pytest.param(
            f"DELETE FROM candles WHERE market_id = {_MARKET_A} AND interval = 60 "
            "AND end_ts = (SELECT MAX(end_ts) FROM candles "
            f"WHERE market_id = {_MARKET_A} AND interval = 60)",
            id="a missing candle",
        ),
        pytest.param(
            "UPDATE candles SET open_interest = 0 "
            f"WHERE (market_id, interval, end_ts) IN {_ONE_NULL_CANDLE}",
            id="a NULL turned into 0",
        ),
    ],
)
def test_any_changed_row_changes_the_digest(
    store: SnapshotStore, tmp_path: Path, change: str
) -> None:
    before = store.content_sha256()
    path = tmp_path / "changed.sqlite"
    shutil.copy(store.path, path)
    changed = SnapshotStore(path)
    assert changed.content_sha256() == before  # the copy holds the same rows
    with changed._tx() as db:
        assert db.execute(change).rowcount == 1
    assert changed.content_sha256() != before
    changed.close()


def test_a_candle_without_a_stored_market_is_refused(store: SnapshotStore) -> None:
    with store._tx() as db:
        db.execute(
            "INSERT INTO candles (market_id, interval, end_ts, volume) VALUES (999, 1, 0, 0)"
        )
    with pytest.raises(ValueError, match="candles row"):
        store.content_sha256()


def test_data_build_prints_the_content_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import pmfa.world.kalshi.production as production_module
    from pmfa.cli.app import app

    toml = tmp_path / "pmfa.toml"
    toml.write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    monkeypatch.setenv("PMFA_CONFIG", str(toml))
    monkeypatch.setattr(
        production_module, "production_exchange", lambda config, clock: FakeExchange()
    )
    result = CliRunner().invoke(
        app, ["data", "build", "--skip-corpus", "--start", "2026-07-01", "--end", "2026-11-25"]
    )
    assert result.exit_code == 0, result.output
    store = SnapshotStore(tmp_path / "data" / "snapshot" / "snapshot.sqlite")
    manifest = store.manifest()
    assert manifest is not None and manifest.content_sha256 == store.content_sha256()
    assert f"snapshot: content SHA-256 {manifest.content_sha256}" in result.output
    store.close()
