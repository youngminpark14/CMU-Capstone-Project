"""The trading pass when two news triggers share one instant and the first one's order fills.

A second Fed statement document is published at the same moment as the first
(the real corpus has such a pair). The first episode buys the hold market,
which fills from the one-minute candle ending two minutes after the trigger.
The second episode must not decide before that fill existed, so it starts at
the fill's time, with that time as its as-of time, and sizes on the holding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmfa.evaluation.manifest import RunManifest
from pmfa.records.decision import OrderResult
from pmfa.records.episode import EpisodeRecord
from pmfa.state.store import SqliteStore
from pmfa.world.evidence.corpus import CorpusManifest, CorpusStore
from tests.evaluation.backtest.support import (
    CALENDAR,
    DOC1_AT,
    DOCUMENTS,
    FED,
    OPEN,
    WINDOW_END,
    build_data,
    document,
    make_config,
    scripted,
)

FIRST = "doc:fed-statement-2026-08-10"
SECOND = "doc:fed-statement-2026-08-10-annex"
FILL_CANDLE_END = DOC1_AT + timedelta(minutes=2)  # arrival 14:01, the minute candle ending 14:02

ANNEX = document(
    "fed-statement-2026-08-10-annex",
    "federalreserve.gov",
    "fomc_statement",
    "Federal Reserve statement annex on the federal funds rate",
    "The Committee will hold the federal funds rate steady at its August decision; the target "
    "range for the federal funds rate is maintained.",
    DOC1_AT,
)


@dataclass
class SameInstant:
    run_dir: Path
    manifest: RunManifest
    episodes: dict[str, EpisodeRecord]
    orders: dict[str, list[OrderResult]]


@pytest.fixture(scope="module")
def same_instant(tmp_path_factory: pytest.TempPathFactory) -> SameInstant:
    from pmfa.evaluation.backtest import run_backtest

    root = tmp_path_factory.mktemp("same-instant")
    config = make_config(root / "data")
    build_data(config)
    CorpusStore(config.paths.corpus_dir()).write(
        [*DOCUMENTS, ANNEX],
        events=CALENDAR,
        manifest=CorpusManifest(built_at=WINDOW_END, window_start=OPEN, window_end=WINDOW_END),
    )
    run_dir = root / "run"
    manifest = run_backtest(
        config,
        run_dir=run_dir,
        systems=["pipeline"],
        score=False,
        models=scripted(config),
        save_examples=10,
    )
    store = SqliteStore(run_dir / "work" / "trading.sqlite")
    episodes = {e.trigger.trigger_id: e for e in store.list_episodes()}
    orders = {
        trigger_id: store.list_orders(episode_id=e.episode_id) for trigger_id, e in episodes.items()
    }
    store.close()
    return SameInstant(run_dir, manifest, episodes, orders)


def test_the_second_episode_starts_once_the_first_fill_was_priced(
    same_instant: SameInstant,
) -> None:
    first, second = same_instant.episodes[FIRST], same_instant.episodes[SECOND]
    [order] = same_instant.orders[FIRST]
    assert first.as_of == order.placed_at == DOC1_AT
    assert order.fills and {f.filled_at for f in order.fills} == {FILL_CANDLE_END}
    assert order.outcome_at == FILL_CANDLE_END
    # The second trigger was published at DOC1_AT too, but its episode ran from the fill's time.
    assert second.as_of == second.trigger.at == FILL_CANDLE_END
    assert second.as_of >= max(f.filled_at for f in order.fills)


def test_the_second_episode_sizes_on_the_first_holding(same_instant: SameInstant) -> None:
    [order] = same_instant.orders[FIRST]
    second = same_instant.episodes[SECOND]
    assert second.proposal is not None and second.proposal.market_ticker == f"{FED}-H0"
    assert second.proposal.position_before == int(order.filled_contracts)
    assert second.proposal.bankroll == Decimal("1000") - order.fees


def test_metrics_count_the_late_start(same_instant: SameInstant) -> None:
    metrics = json.loads((same_instant.run_dir / "metrics.json").read_text())
    assert metrics["late_starts"] == {
        "episodes": 1,
        "of": len(same_instant.episodes),
        "longest_seconds": 120.0,
    }


def test_the_saved_trace_of_the_late_episode_replays_and_matches(
    same_instant: SameInstant,
) -> None:
    from pmfa.examples import check, replay

    second = same_instant.episodes[SECOND]
    [(relative, status)] = [
        (r, s) for r, s in same_instant.manifest.traces.items() if r.endswith(second.episode_id)
    ]
    assert status == "replays and matches"
    result = replay(same_instant.run_dir / relative)
    assert check(result) == []
    assert result.episode.proposal is not None
    assert result.episode.proposal.position_before == second.proposal.position_before  # type: ignore[union-attr]
