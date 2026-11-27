"""StateService: beams, rules, retrieval pass-through, and missing ports."""

from __future__ import annotations

import pytest

from pmfa.errors import NotFound
from pmfa.records.forecast import NodeStatus
from pmfa.state.service import OPERATIONS, PortUnavailable, StateService
from pmfa.state.store import SqliteStore

from . import factories as f


@pytest.fixture
def knowledge():
    return f.FakeKnowledge()


@pytest.fixture
def service(knowledge):
    with SqliteStore(":memory:") as store:
        yield StateService(store, knowledge=knowledge, exchange=f.FakeExchange())


def test_operations_list_names_every_public_method():
    public = {
        name
        for name in vars(StateService)
        if not name.startswith("_") and callable(getattr(StateService, name))
    }
    assert public == set(OPERATIONS)


def test_beam_groups_placed_scenarios_by_depth_and_mass(service):
    nodes = [
        f.node("a", depth=1, status=NodeStatus.EXPANDED, mass=0.5),
        f.node("b", depth=1, status=NodeStatus.EXPANDED, mass=0.3),
        f.node("c", depth=1, status=NodeStatus.FROZEN, mass=0.1),
        f.node("d", depth=1, status=NodeStatus.DELETED),
        f.node("e", depth=2, status=NodeStatus.LEAF, mass=0.2, parent_id="b"),
        f.node("g", depth=2, status=NodeStatus.LEAF, mass=0.25, parent_id="a"),
        f.node("h", depth=2, status=NodeStatus.REJECTED, parent_id="a"),
        f.node("i", depth=3, status=NodeStatus.SCORED, parent_id="g"),
    ]
    for n in nodes:
        service.put_node(n)
    beam = service.get_beam("ep-1")
    assert beam.depth == 2
    level1, level2 = beam.levels
    assert [n.node_id for n in level1.expanded] == ["a", "b"]
    assert [n.node_id for n in level1.frozen] == ["c"]
    assert level1.leaf == []
    assert [n.node_id for n in level2.leaf] == ["g", "e"]
    assert service.get_beam("empty").depth == 0


def test_nodes_by_id_and_depth(service):
    service.put_node(f.node("a", depth=1))
    service.put_node(f.node("b", depth=2))
    assert service.get_node("ep-1", "b") == f.node("b", depth=2)
    assert [n.node_id for n in service.get_nodes("ep-1", depth=2)] == ["b"]
    with pytest.raises(NotFound):
        service.get_node("ep-1", "zzz")


def test_rules_come_from_the_exchange_without_prices(service):
    rules = service.get_rules(f.EVENT)
    assert rules.event_ticker == f.EVENT and rules.series_ticker == "KXFEDDECISION"
    assert [m.ticker for m in rules.markets] == [f"{f.EVENT}-H0", f"{f.EVENT}-C25"]
    assert rules.markets[0].rules_primary == "If the Fed holds, resolves Yes."
    assert "yes_bid" not in rules.model_dump_json()
    with pytest.raises(NotFound):
        service.get_rules("NOPE")


def test_as_of_reaches_the_knowledge_port_unchanged(service, knowledge):
    as_of, after = f.at(1), f.at(-24)
    service.search_events("fed hold", as_of=as_of, k=3)
    service.search_evidence(["for", "against"], as_of=as_of, category="fed", published_after=after)
    service.search_ledger("fed", as_of=as_of, category="fed")
    assert [p.passage_id for p in service.get_passages(["p-1", "p-future"], as_of=as_of)] == ["p-1"]
    assert knowledge.calls == [
        ("search_events", {"query": "fed hold", "as_of": as_of, "k": 3}),
        (
            "search_evidence",
            {
                "queries": ["for", "against"],
                "as_of": as_of,
                "category": "fed",
                "k": 8,
                "published_after": after,
            },
        ),
        ("search_ledger", {"query": "fed", "as_of": as_of, "category": "fed", "k": 5}),
        ("get_passages", {"passage_ids": ["p-1", "p-future"], "as_of": as_of}),
    ]
    assert all(call[1]["as_of"] is as_of for call in knowledge.calls)


def test_as_of_is_required_and_must_be_aware(service, knowledge):
    with pytest.raises(TypeError):
        service.search_events("fed")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="timezone-aware"):
        service.search_events("fed", as_of=f.T0.replace(tzinfo=None))
    assert knowledge.calls == []


def test_operations_without_their_port_raise_a_clear_error():
    with SqliteStore(":memory:") as store:
        bare = StateService(store)
        with pytest.raises(PortUnavailable, match="Knowledge"):
            bare.search_events("fed", as_of=f.T0)
        with pytest.raises(PortUnavailable, match="Knowledge"):
            bare.get_passages(["p-1"], as_of=f.T0)
        with pytest.raises(PortUnavailable, match="Exchange"):
            bare.get_rules(f.EVENT)
        assert bare.get_halt().halted is False  # store-backed operations still work


def test_episode_reads(service):
    with pytest.raises(NotFound):
        service.get_episode("ep-1")
    service.store.save_episode(f.episode())
    service.store.append_trace("ep-1", f.step(1))
    assert service.get_episode("ep-1").trace == [f.step(1)]
    assert service.get_trace("ep-1") == [f.step(1)]
    assert [e.episode_id for e in service.list_episodes()] == ["ep-1"]
