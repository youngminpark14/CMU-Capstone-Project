"""The MCP server: every tool through the in-memory client, on whichever SDK line is installed."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pmfa.records.base import Stage
from pmfa.records.forecast import NodeStatus
from pmfa.state import mcp_compat
from pmfa.state.client import SyncBridge, ToolCallError, connect_in_memory
from pmfa.state.server import build_server
from pmfa.state.service import OPERATIONS, READ_OPERATIONS, PortUnavailable, StateService
from pmfa.state.store import SqliteStore

from . import factories as f


class Mcp:
    """A synchronous handle on an in-memory MCP client, for tests."""

    def __init__(self, bridge: SyncBridge, client: Any) -> None:
        self.bridge = bridge
        self.client = client

    def call(self, name: str, **arguments: Any) -> Any:
        return self.bridge.call(self.client.call_tool, name, arguments)

    def read(self, uri: str) -> Any:
        return json.loads(self.bridge.call(self.client.read_resource, uri))


@pytest.fixture
def knowledge():
    return f.FakeKnowledge()


@pytest.fixture
def service(knowledge):
    with SqliteStore(":memory:") as store:
        store.save_episode(f.episode())
        for seq in (1, 2, 3):
            store.append_trace("ep-1", f.step(seq))
        store.add_ledger_entries([f.ledger_entry("e-1", 0.8)])
        store.resolve_market(f.MARKET, True, f.at(1))
        store.set_stage(Stage.SHADOW, "benchmarks met", f.at(1))
        yield StateService(store, knowledge=knowledge, exchange=f.FakeExchange())


@pytest.fixture
def mcp(service):
    bridge = SyncBridge()
    try:
        yield Mcp(bridge, bridge.enter(connect_in_memory(build_server(service))))
    finally:
        bridge.close()


def _json(value: Any) -> Any:
    """What a record (or list of records) looks like as plain JSON."""
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value.model_dump(mode="json")


def test_tools_map_one_to_one_onto_service_methods(mcp):
    tools = mcp.client.tools
    assert set(tools) == set(OPERATIONS)
    for name, tool in tools.items():
        assert tool.description, name
        assert tool.output_schema is not None, name
        assert tool.read_only is (name in READ_OPERATIONS), name


def test_resource_templates_are_listed(mcp):
    templates = mcp.bridge.call(mcp.client.list_resource_templates)
    assert set(templates) == {"episode://{episode_id}", "episode://{episode_id}/trace"}


def test_tree_tools(mcp, service):
    a = f.node("a", status=NodeStatus.EXPANDED, mass=0.6)
    b = f.node("b", status=NodeStatus.FROZEN, mass=0.1)
    assert mcp.call("put_node", node=_json(a)) == _json(a)
    mcp.call("put_node", node=_json(b))
    assert mcp.call("get_node", episode_id="ep-1", node_id="b") == _json(b)
    assert mcp.call("get_nodes", episode_id="ep-1") == _json([a, b])
    assert mcp.call("get_nodes", episode_id="ep-1", depth=2) == []
    assert mcp.call("get_beam", episode_id="ep-1") == _json(service.get_beam("ep-1"))
    assert mcp.call(
        "record_rejection", episode_id="ep-1", event_ticker=f.EVENT, claim="hike", reason="no"
    ) == {"claim": "hike", "reason": "no"}
    assert mcp.call("get_rejections", event_ticker=f.EVENT) == [{"claim": "hike", "reason": "no"}]


def test_rules_and_retrieval_tools(mcp, service, knowledge):
    as_of = "2026-11-02T16:00:00Z"
    assert mcp.call("get_rules", event_ticker=f.EVENT) == _json(service.get_rules(f.EVENT))
    assert mcp.call("search_events", query="fed", as_of=as_of)[0]["event_ticker"] == f.EVENT
    hits = mcp.call("search_evidence", queries=["for", "against"], as_of=as_of, category="fed")
    assert hits[0]["passage"]["passage_id"] == "p-1"
    assert mcp.call("search_ledger", query="fed", as_of=as_of, category="fed")[0]["outcome"]
    assert [
        p["passage_id"] for p in mcp.call("get_passages", passage_ids=["p-1"], as_of=as_of)
    ] == ["p-1"]
    # Each call reached the Knowledge port with the as-of time the caller sent.
    assert [call[1]["as_of"] for call in knowledge.calls] == [f.at(1)] * 4


def test_ledger_and_calibration_tools(mcp, service):
    assert mcp.call("query_ledger") == _json(service.query_ledger())
    assert mcp.call("query_ledger", category="cpi") == []
    assert mcp.call("calibration", category="fed", as_of="2026-11-03T00:00:00Z") == {
        "category": "fed",
        "n_resolved": 1,
        "calibration_error": pytest.approx(0.2),
    }


def test_episode_tools_and_resources(mcp, service):
    episode = _json(service.get_episode("ep-1"))
    assert [s["seq"] for s in episode["trace"]] == [1, 2, 3]
    assert mcp.call("get_episode", episode_id="ep-1") == episode
    assert mcp.call("list_episodes") == [episode]
    assert mcp.call("list_episodes", status="done") == []
    assert mcp.call("get_trace", episode_id="ep-1") == episode["trace"]
    assert mcp.read("episode://ep-1") == episode
    assert mcp.read("episode://ep-1/trace") == episode["trace"]


def test_halt_and_stage_tools(mcp):
    assert mcp.call("get_halt") == {"halted": False, "reason": "", "at": None}
    assert mcp.call("get_stage")["stage"] == "shadow"


def test_errors_reach_the_caller_with_their_kind(mcp):
    from pmfa.errors import NotFound

    with pytest.raises(NotFound, match="no episode 'nope'"):
        mcp.call("get_episode", episode_id="nope")
    with pytest.raises(ValueError, match="timezone-aware"):
        mcp.call("search_events", query="fed", as_of="2026-11-02T16:00:00")
    with pytest.raises(ToolCallError):
        mcp.call("search_events", query="fed")  # as_of is required by the schema
    with pytest.raises(Exception, match="nope"):
        mcp.read("episode://nope")


def test_missing_port_is_a_clear_tool_error():
    with SqliteStore(":memory:") as store:
        bridge = SyncBridge()
        try:
            client = bridge.enter(connect_in_memory(build_server(StateService(store))))
            with pytest.raises(PortUnavailable, match="Exchange"):
                bridge.call(client.call_tool, "get_rules", {"event_ticker": f.EVENT})
        finally:
            bridge.close()


def test_sdk_line_is_detected():
    import mcp.server

    assert mcp_compat.sdk_major() == (2 if hasattr(mcp.server, "MCPServer") else 1)
