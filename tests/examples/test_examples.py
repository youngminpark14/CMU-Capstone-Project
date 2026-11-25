"""The committed sample episodes replay offline to exactly their expected/ files.

Nothing is normalised in the comparison: the replay runs on a simulated clock
and every id is derived from the inputs, so no field of the episode record or
the review card is volatile.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pmfa.cli.app import app
from pmfa.errors import CassetteMiss
from pmfa.examples import HAND_MADE_NOTICE, check, list_examples, replay
from pmfa.records.episode import Exit
from pmfa.world.recorded import load_recorded_world

ROOT = Path(__file__).resolve().parents[2]
EPISODES = ROOT / "examples" / "episodes"
EXPECTED_EXITS = {
    "fed-minutes-hawkish": Exit.EXECUTED,
    "phantom-edge-no-trade": Exit.NO_EDGE,
    "review-resize": Exit.EXECUTED,
    "cpi-ladder": Exit.NO_EDGE,
    "single-origin-syndicated": Exit.INSUFFICIENT_ORIGINS,
}
runner = CliRunner()


def copy_example(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copytree(EPISODES / name, target)
    return target


def test_the_five_examples_are_committed():
    assert [p.name for p in list_examples(EPISODES)] == sorted(EXPECTED_EXITS)


@pytest.mark.parametrize("name", sorted(EXPECTED_EXITS))
def test_each_example_replays_offline_to_exactly_its_expected_files(name):
    result = replay(EPISODES / name)
    assert check(result) == []
    assert result.episode.exit is EXPECTED_EXITS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_EXITS))
def test_each_example_says_it_is_a_hand_made_fixture(name):
    file = load_recorded_world(EPISODES / name / "world.json")
    assert file.provenance == "hand-made fixture"
    readme = (EPISODES / name / "README.md").read_text(encoding="utf-8")
    assert "hand-made fixture with hand-written model records" in readme
    assert "the author knew" in readme  # the outcome was known when the records were written
    lines = [line for line in readme.splitlines() if line.strip()]
    assert 5 <= len(lines) <= 10
    cassette = json.loads((EPISODES / name / "cassette.json").read_text(encoding="utf-8"))
    assert {e["source"] for e in cassette["entries"]} == {"scripted"}


def test_the_example_with_a_review_keeps_its_card_and_the_resize_shrinks_the_order():
    result = replay(EPISODES / "review-resize")
    review = result.episode.review
    assert review is not None and review.decision.value == "resize"
    assert review.reason_code is not None and review.reason_code.value == "size_too_large"
    assert result.card is not None and result.card.contracts > review.contracts
    [order] = result.episode.orders
    assert order.requested_contracts == review.contracts == order.filled_contracts
    assert (EPISODES / "review-resize" / "expected" / "review_card.json").is_file()


def test_the_ladder_forecast_is_monotone_across_strikes():
    forecast = replay(EPISODES / "cpi-ladder").episode.forecast
    assert forecast is not None
    probs = list(forecast.market_probabilities.values())
    assert len(probs) == 7 and all(a >= b for a, b in itertools.pairwise(probs))


def test_the_replay_of_a_hand_made_fixture_prints_the_provenance_line_first():
    out = runner.invoke(app, ["replay", str(EPISODES / "fed-minutes-hawkish")])
    assert out.exit_code == 0, out.output
    first = next(line for line in out.output.splitlines() if line.strip())
    assert first.startswith(HAND_MADE_NOTICE.split(":")[0])
    flat = " ".join(out.output.split())
    assert HAND_MADE_NOTICE in flat
    for heading in ("Trace", "Forecast", "Decision", "Orders and fills", "Exit"):
        assert heading in out.output
    assert "\x1b[" not in out.output  # no colour codes when not a terminal


def test_check_passes_on_a_committed_example_and_json_prints_the_record():
    out = runner.invoke(app, ["replay", "cpi-ladder", "--check", "--json"])
    assert out.exit_code == 0, out.output
    record = json.loads(out.stdout)
    assert record["exit"] == "no_edge"


def test_check_fails_when_an_expected_file_is_edited(tmp_path):
    folder = copy_example("phantom-edge-no-trade", tmp_path)
    path = folder / "expected" / "episode.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["exit_detail"] = "edited by hand"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    out = runner.invoke(app, ["replay", str(folder), "--check"])
    assert out.exit_code == 1
    assert "exit_detail" in out.output


def test_check_fails_when_the_review_card_is_edited(tmp_path):
    folder = copy_example("review-resize", tmp_path)
    path = folder / "expected" / "review_card.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["contracts"] = 1
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    out = runner.invoke(app, ["replay", str(folder), "--check"])
    assert out.exit_code == 1 and "review_card.json" in out.output


def test_a_cassette_whose_recorded_input_was_altered_stops_the_replay(tmp_path):
    folder = copy_example("fed-minutes-hawkish", tmp_path)
    path = folder / "cassette.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"][1]["input"]["event"]["title"] = "An altered recording"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with pytest.raises(CassetteMiss, match="input differs"):
        replay(folder)
    out = runner.invoke(app, ["replay", str(folder)])
    assert out.exit_code == 3
    assert "replay stopped" in out.output


def test_list_shows_every_example_on_one_line():
    out = runner.invoke(app, ["replay", "--list"])
    assert out.exit_code == 0
    lines = out.output.strip().splitlines()
    assert len(lines) == len(EXPECTED_EXITS)
    for name, expected in EXPECTED_EXITS.items():
        assert any(line.startswith(name) and expected.value in line for line in lines)


def test_trace_prints_a_stored_episode_from_a_state_database(tmp_path):
    from pmfa.state.store import SqliteStore

    result = replay(EPISODES / "review-resize")
    store = SqliteStore(tmp_path / "state.sqlite")
    episode = result.episode
    store.save_episode(episode)
    for step in episode.trace:
        store.append_trace(episode.episode_id, step)
    store.add_ledger_entries(result.ledger)
    out = runner.invoke(app, ["trace", episode.episode_id, "--db", str(tmp_path / "state.sqlite")])
    assert out.exit_code == 0, out.output
    assert "Stored episode" in out.output and "executed" in out.output
    assert "KXFEDDECISION-26OCT-H0" in out.output
    missing = runner.invoke(app, ["trace", "nope", "--db", str(tmp_path / "state.sqlite")])
    assert missing.exit_code == 2


GUARD = textwrap.dedent(
    """
    import socket, sys

    def refuse(*args, **kwargs):
        raise OSError("network access during replay")

    original = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse()
        return original(self, address)

    socket.socket.connect = connect
    socket.create_connection = refuse
    sys.argv = ["pmfa", "replay", sys.argv[1], "--check"]
    from pmfa.cli.app import app
    try:
        app()
    except SystemExit as exc:
        code = exc.code or 0
    else:
        code = 0
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("anthropic", "langchain_anthropic", "crewai"))
    print("LOADED", loaded)
    sys.exit(code)
    """
)


def test_replay_runs_with_no_key_no_proxy_and_no_network(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("API_KEY")}
    closed = "http://127.0.0.1:9"  # the discard port: nothing listens
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env[name] = closed
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)
    script = tmp_path / "guarded_replay.py"
    script.write_text(GUARD, encoding="utf-8")
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(script), str(EPISODES / "review-resize")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.monotonic() - started
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "check passed" in done.stdout + done.stderr
    assert "LOADED []" in done.stdout  # no model client was imported
    assert elapsed < 30, f"replay took {elapsed:.1f}s"
