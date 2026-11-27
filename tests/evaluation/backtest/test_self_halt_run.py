"""A backtest that halts itself in both passes: the episodes file and the halt blocks agree.

The tiny world's stop conditions are set so low that the first loss trips
them. Two Fed notes are added a few seconds after the Fed statement, so both
wait for the statement's order outcome and start at the same time with the
same as-of time. The pass runs them in the schedule's order, by publication
time; their doc ids sort the other way on purpose, so only the run knows
which ran first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pmfa.evaluation.schedule import build_schedule
from pmfa.world.backtest import BacktestData
from pmfa.world.evidence.corpus import CorpusStore
from tests.evaluation.backtest.support import (
    CALENDAR,
    DOC1_AT,
    DOCUMENTS,
    build_data,
    document,
    make_config,
    scripted,
)

TINY = Decimal("0.00001")
FIRST_NOTE, SECOND_NOTE = "fed-note-z", "fed-note-a"


@dataclass
class HaltedRun:
    run_dir: Path
    ran: list[str]
    """Trigger ids in the order the run made them: scoring points, then trading triggers."""
    episodes: list[dict[str, Any]]
    metrics: dict[str, Any]


def _note(doc_id: str, seconds: int) -> Any:
    return document(
        doc_id,
        "federalreserve.gov",
        "fomc_statement",
        f"Federal Reserve note {doc_id} on the federal funds rate",
        "The Committee will hold the federal funds rate steady in August. " + DOCUMENTS[0].text,
        DOC1_AT + timedelta(seconds=seconds),
    )


@pytest.fixture(scope="module")
def halted(tmp_path_factory: pytest.TempPathFactory) -> HaltedRun:
    from pmfa.evaluation.backtest import run_backtest

    root = tmp_path_factory.mktemp("halted")
    config = make_config(root / "data")
    caps = config.caps.model_copy(
        update={"drawdown_halt_fraction": TINY, "daily_loss_stop_fraction": TINY}
    )
    config = config.model_copy(update={"caps": caps})
    build_data(config)
    corpus = CorpusStore(config.paths.corpus_dir())
    notes = [_note(FIRST_NOTE, 20), _note(SECOND_NOTE, 40)]
    corpus.write([*DOCUMENTS, *notes], events=CALENDAR, manifest=corpus.manifest())

    data = BacktestData(config)
    schedule = build_schedule(data.events(), data.corpus, config, window_end=data.window_end)
    data.close()
    ran = [p.trigger().trigger_id for p in schedule.points]
    ran += [t.trigger_id for t in schedule.triggers]

    run_dir = root / "run"
    run_backtest(config, run_dir=run_dir, systems=["pipeline"], models=scripted(config))
    lines = (run_dir / "episodes.jsonl").read_text().splitlines()
    metrics = json.loads((run_dir / "metrics.json").read_text())
    return HaltedRun(run_dir, ran, [json.loads(line) for line in lines], metrics)


def _pass(run: HaltedRun, name: str) -> list[dict[str, Any]]:
    scoring = name == "scoring"
    return [e for e in run.episodes if (e["trigger"]["kind"] == "schedule") is scoring]


def test_the_episodes_file_lists_the_episodes_in_the_order_they_ran(halted: HaltedRun) -> None:
    assert [e["trigger"]["trigger_id"] for e in halted.episodes] == halted.ran
    # The two notes waited for the same outcome: one start, one as-of time, ids sorting backwards.
    first, second = (
        next(e for e in halted.episodes if e["trigger"]["doc_id"] == d)
        for d in (FIRST_NOTE, SECOND_NOTE)
    )
    assert first["started_at"] == second["started_at"] == first["as_of"] == second["as_of"]
    assert datetime.fromisoformat(first["as_of"]) > DOC1_AT + timedelta(seconds=40)
    assert SECOND_NOTE < FIRST_NOTE


@pytest.mark.parametrize("name", ["scoring", "trading"])
def test_each_pass_names_its_first_halted_episode_in_the_file(halted: HaltedRun, name: str) -> None:
    episodes = _pass(halted, name)
    first = next(i for i, e in enumerate(episodes) if e["exit"] == "halted")
    head, later = episodes[first], episodes[first:]
    block = halted.metrics["self_halts"][name]
    assert block["first_episode_id"] == head["episode_id"]
    assert block["first_trigger_id"] == head["trigger"]["trigger_id"]
    assert datetime.fromisoformat(block["first_started_at"]) == datetime.fromisoformat(
        head["started_at"]
    )
    assert block["reason"] == head["exit_detail"]
    assert block["n_from_then"] == len(later)
    assert block["n_halted_from_then"] == sum(e["exit"] == "halted" for e in later)
    if name == "trading":
        assert block["first_trigger_id"] == f"doc:{FIRST_NOTE}"


def _section(text: str, heading: str) -> str:
    return text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def test_the_report_gives_each_passs_halt_in_that_passs_own_part(halted: HaltedRun) -> None:
    from pmfa.evaluation.report import build_report

    text = build_report(halted.run_dir).read_text()
    exits = _section(text, "Exits by reason")
    parts = {
        "scoring": exits.split("Scoring pass", 1)[1].split("Trading pass", 1)[0],
        "trading": exits.split("Trading pass", 1)[1],
    }
    blocks = halted.metrics["self_halts"]
    for name, other in (("scoring", "trading"), ("trading", "scoring")):
        block, part = blocks[name], parts[name]
        assert block["first_started_at"] in part
        assert block["first_trigger_id"] in part
        assert block["reason"] in part
        assert f"{block['n_halted_from_then']} of the {block['n_from_then']}" in part
        assert blocks[other]["first_trigger_id"] not in part
    trading = _section(text, "Simulated trading after fees")
    assert blocks["trading"]["first_started_at"] in trading
    assert blocks["scoring"]["first_started_at"] not in trading
