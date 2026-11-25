"""StateClient: the direct and mcp flavours give the same answers; the bridge is safe in a loop."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from pmfa.errors import NotFound
from pmfa.records.forecast import NodeStatus
from pmfa.state.client import StateClient, SyncBridge
from pmfa.state.service import OPERATIONS, PortUnavailable, StateService
from pmfa.state.store import SqliteStore

from . import factories as f


@pytest.fixture
def service():
    with SqliteStore(":memory:") as store:
        store.save_episode(f.episode())
        store.save_episode(f.episode("ep-2", started_at=f.at(1)))
        for seq in (2, 1, 3):
            store.append_trace("ep-1", f.step(seq))
        store.add_ledger_entries(
            [f.ledger_entry("e-1", 0.8), f.ledger_entry("e-2", 0.3, market_price=None)]
        )
        store.resolve_market(f.MARKET, False, f.at(2))
        store.set_halt("drawdown", f.at(3))
        yield StateService(store, knowledge=f.FakeKnowledge(), exchange=f.FakeExchange())


@pytest.fixture
def clients(service):
    direct = StateClient.direct(service)
    with StateClient.mcp(service) as over_mcp:
        yield direct, over_mcp


def test_state_client_has_the_service_methods_with_the_same_signatures():
    public = {n for n in vars(StateClient) if not n.startswith("_")}
    assert set(OPERATIONS) <= public
    for name in OPERATIONS:
        ours = inspect.signature(getattr(StateClient, name))
        theirs = inspect.signature(getattr(StateService, name))
        assert ours == theirs, name


def test_direct_and_mcp_flavours_agree(clients):
    direct, over_mcp = clients
    assert direct.flavour == "direct" and over_mcp.flavour == "mcp"
    as_of = f.at(4)

    # Writes go through one flavour and are seen by both.
    over_mcp.put_node(f.node("a", status=NodeStatus.EXPANDED, mass=0.5))
    direct.put_node(f.node("b", status=NodeStatus.FROZEN, mass=0.2))
    over_mcp.put_node(f.node("c", depth=2, status=NodeStatus.LEAF, mass=0.3, parent_id="a"))
    over_mcp.record_rejection("ep-1", f.EVENT, "hike", "contradicts the minutes")

    reads = [
        lambda c: c.put_node(f.node("d", depth=2, status=NodeStatus.DELETED)),
        lambda c: c.get_node("ep-1", "a"),
        lambda c: c.get_nodes("ep-1"),
        lambda c: c.get_nodes("ep-1", depth=2),
        lambda c: c.get_beam("ep-1"),
        lambda c: c.record_rejection("ep-1", f.EVENT, "cut 50", "no support"),
        lambda c: c.get_rejections(f.EVENT),
        lambda c: c.get_rules(f.EVENT),
        lambda c: c.get_passages(["p-1", "p-2"], as_of=as_of),
        lambda c: c.search_events("fed", as_of=as_of, k=2),
        lambda c: c.search_evidence(["a", "b"], as_of=as_of, category="fed", published_after=f.T0),
        lambda c: c.search_ledger("fed", as_of=as_of, category="fed"),
        lambda c: c.query_ledger(),
        lambda c: c.query_ledger(resolved_before=as_of, category="fed", system="pipeline"),
        lambda c: c.calibration("fed", as_of=as_of),
        lambda c: c.list_episodes(),
        lambda c: c.list_episodes(event_ticker=f.EVENT, limit=1),
        lambda c: c.get_episode("ep-1"),
        lambda c: c.get_trace("ep-1"),
        lambda c: c.get_halt(),
        lambda c: c.get_stage(),
    ]
    for read in reads:
        assert read(direct) == read(over_mcp)
    assert [s.seq for s in over_mcp.get_trace("ep-1")] == [2, 1, 3]
    assert over_mcp.get_halt().at == f.at(3)


def test_both_flavours_raise_the_same_errors(clients, service):
    for client in clients:
        with pytest.raises(NotFound):
            client.get_episode("nope")
        with pytest.raises(NotFound):
            client.get_node("ep-1", "nope")
        with pytest.raises(ValueError, match="timezone-aware"):
            client.calibration("fed", as_of=f.T0.replace(tzinfo=None))
    bare = StateService(service.store)
    with StateClient.mcp(bare) as over_mcp:
        for client in (StateClient.direct(bare), over_mcp):
            with pytest.raises(PortUnavailable):
                client.search_events("fed", as_of=f.T0)


def test_sync_bridge_works_from_inside_a_running_event_loop(service):
    async def caller() -> list[str]:
        # Synchronous code called from async code: no nested asyncio.run.
        with StateClient.mcp(service) as client:
            return [e.episode_id for e in client.list_episodes()]

    assert asyncio.run(caller()) == ["ep-2", "ep-1"]


def test_sync_bridge_runs_coroutines_on_its_own_thread():
    import threading

    async def where() -> str:
        return threading.current_thread().name

    bridge = SyncBridge()
    try:
        assert bridge.call(where) != threading.current_thread().name
    finally:
        bridge.close()
