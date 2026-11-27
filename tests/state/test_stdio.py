"""Smoke test: ``python -m pmfa.state --db PATH`` serves MCP over stdio."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio

from pmfa.state.client import connect_stdio
from pmfa.state.service import OPERATIONS
from pmfa.state.store import SqliteStore

from . import factories as f

SRC = Path(__file__).resolve().parents[2] / "src"


def test_stdio_server_lists_tools_and_answers_a_call(tmp_path):
    db = tmp_path / "state.sqlite"
    with SqliteStore(db) as store:
        store.save_episode(f.episode())
        store.set_halt("manual", f.at(1))

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH", "")]))

    async def session() -> tuple[set[str], dict, list[str]]:
        args = ["-m", "pmfa.state", "--db", str(db)]
        with anyio.fail_after(60):
            async with connect_stdio(sys.executable, args, env=env) as client:
                halt = await client.call_tool("get_halt", {})
                episodes = await client.call_tool("list_episodes", {})
                return set(client.tools), halt, [e["episode_id"] for e in episodes]

    names, halt, episodes = anyio.run(session)
    assert names == set(OPERATIONS)
    assert halt["halted"] is True and halt["reason"] == "manual"
    assert episodes == ["ep-1"]
