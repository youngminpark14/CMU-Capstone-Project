"""SqliteStore: every StateStore method round-trips its records, and the arithmetic is right."""

from __future__ import annotations

import inspect
import sqlite3
import threading
from decimal import Decimal

import pytest

from pmfa.errors import NotFound
from pmfa.ports import StateStore
from pmfa.records.base import Stage
from pmfa.records.decision import ProposalStatus, ReasonCode, Review, ReviewDecision
from pmfa.records.episode import EpisodeStatus, Exit, HaltState, StageState
from pmfa.records.forecast import NodeStatus
from pmfa.records.market import Side
from pmfa.records.roles import RejectionBrief
from pmfa.state.schema import SCHEMA_VERSION
from pmfa.state.store import SchemaMismatch, SqliteStore

from . import factories as f


@pytest.fixture
def store():
    with SqliteStore(":memory:") as s:
        yield s


def _public_methods(cls: type) -> set[str]:
    return {name for name in vars(cls) if not name.startswith("_") and callable(getattr(cls, name))}


def test_implements_every_state_store_method_with_the_same_signature():
    for name in _public_methods(StateStore):
        ours = inspect.signature(getattr(SqliteStore, name))
        theirs = inspect.signature(getattr(StateStore, name))
        assert list(ours.parameters) == list(theirs.parameters), name
        for param in theirs.parameters:
            assert ours.parameters[param].kind == theirs.parameters[param].kind, (name, param)
            assert ours.parameters[param].default == theirs.parameters[param].default, (name, param)


# -- episodes and traces


def test_episode_round_trips_without_its_trace_and_comes_back_with_the_stored_one(store):
    ep = f.episode(trace=[f.step(99)], passage_ids=["p-1"], forecast=f.forecast())
    store.save_episode(ep)
    assert store.get_episode("ep-1") == ep.model_copy(update={"trace": []})
    store.append_trace("ep-1", f.step(1))
    assert store.get_episode("ep-1").trace == [f.step(1)]
    assert store.get_episode("missing") is None


def test_save_episode_replaces_and_keeps_the_trace(store):
    store.save_episode(f.episode())
    store.append_trace("ep-1", f.step(1))
    done = f.episode(status=EpisodeStatus.DONE, exit=Exit.NO_EDGE, finished_at=f.at(1))
    store.save_episode(done)
    got = store.get_episode("ep-1")
    assert got.exit is Exit.NO_EDGE and got.status is EpisodeStatus.DONE
    assert got.trace == [f.step(1)]


def test_trace_keeps_the_order_steps_were_appended(store):
    store.save_episode(f.episode())
    steps = [f.step(3), f.step(1), f.step(2), f.step(10)]
    for s in steps:
        store.append_trace("ep-1", s)
    store.append_trace("ep-other", f.step(5))
    assert store.get_trace("ep-1") == steps
    assert store.get_trace("ep-other") == [f.step(5)]
    assert store.get_trace("nobody") == []


def test_list_episodes_filters_newest_first_with_traces(store):
    store.save_episode(f.episode("ep-a", started_at=f.at(0)))
    store.save_episode(f.episode("ep-b", started_at=f.at(2), status=EpisodeStatus.DONE))
    store.save_episode(f.episode("ep-c", started_at=f.at(1), event_ticker="OTHER"))
    store.append_trace("ep-b", f.step(1))
    assert [e.episode_id for e in store.list_episodes()] == ["ep-b", "ep-c", "ep-a"]
    assert [e.episode_id for e in store.list_episodes(status=EpisodeStatus.DONE)] == ["ep-b"]
    assert [e.episode_id for e in store.list_episodes(event_ticker=f.EVENT)] == ["ep-b", "ep-a"]
    assert [e.episode_id for e in store.list_episodes(limit=1)] == ["ep-b"]
    assert store.list_episodes(status=EpisodeStatus.DONE)[0].trace == [f.step(1)]


# -- tree


def test_nodes_round_trip_and_update_in_place(store):
    a, b = f.node("n-a", depth=2), f.node("n-b", depth=1)
    store.put_node(a)
    store.put_node(b)
    store.put_node(f.node("n-x", episode_id="ep-2"))
    assert store.get_nodes("ep-1") == [b, a]
    frozen = a.model_copy(update={"status": NodeStatus.FROZEN, "reason": "outside beam"})
    store.put_node(frozen)
    assert store.get_nodes("ep-1") == [b, frozen]


def test_rejections_by_event_kept_once(store):
    store.record_rejection("ep-1", f.EVENT, "a hike", "contradicts the minutes")
    store.record_rejection("ep-2", f.EVENT, "a hike", "contradicts the minutes")
    store.record_rejection("ep-2", f.EVENT, "a 50bp cut", "no support")
    store.record_rejection("ep-3", "OTHER", "x", "y")
    assert store.get_rejections(f.EVENT) == [
        RejectionBrief(claim="a hike", reason="contradicts the minutes"),
        RejectionBrief(claim="a 50bp cut", reason="no support"),
    ]


# -- forecasts, proposals, reviews


def test_last_forecast_time_counts_pipeline_forecasts_only(store):
    assert store.last_forecast_time(f.EVENT) is None
    store.save_forecast(f.forecast("fc-1", as_of=f.at(1)))
    store.save_forecast(f.forecast("fc-2", as_of=f.at(3)))
    store.save_forecast(f.forecast("fc-3", as_of=f.at(5), system="prompt_only"))
    assert store.last_forecast_time(f.EVENT) == f.at(3)
    assert store.last_forecast_time("OTHER") is None


def test_proposal_moves_through_its_statuses(store):
    p, c = f.proposal(), f.card()
    store.save_proposal(p, ProposalStatus.PENDING, c)
    assert store.get_proposal("pr-1") == (p, ProposalStatus.PENDING, c)
    assert store.list_proposals(ProposalStatus.PENDING) == [(p, ProposalStatus.PENDING)]

    store.set_proposal_status("pr-1", ProposalStatus.APPROVED)
    assert store.get_proposal("pr-1") == (p, ProposalStatus.APPROVED, c)
    assert store.list_proposals(ProposalStatus.PENDING) == []

    resized = p.model_copy(update={"contracts": 3})
    store.save_proposal(resized, ProposalStatus.RESIZED)  # no card: the stored card stays
    assert store.get_proposal("pr-1") == (resized, ProposalStatus.RESIZED, c)

    store.set_proposal_status("pr-1", ProposalStatus.EXECUTED)
    store.save_proposal(f.proposal("pr-2"), ProposalStatus.PENDING)
    store.set_proposal_status("pr-2", ProposalStatus.EXPIRED)
    assert store.list_proposals() == [
        (resized, ProposalStatus.EXECUTED),
        (f.proposal("pr-2"), ProposalStatus.EXPIRED),
    ]
    assert store.get_proposal("pr-2")[2] is None
    assert store.get_proposal("missing") is None
    with pytest.raises(NotFound):
        store.set_proposal_status("missing", ProposalStatus.CANCELLED)


def test_reviews_round_trip_in_order(store):
    reviews = [
        Review(proposal_id="pr-1", decision=ReviewDecision.RESIZE, contracts=3, decided_at=f.at(1)),
        Review(
            proposal_id="pr-2",
            decision=ReviewDecision.REJECT,
            reason_code=ReasonCode.STALE_EVIDENCE,
            note="old",
            decided_at=f.at(2),
        ),
    ]
    for r in reviews:
        store.save_review(r)
    assert store.list_reviews() == reviews


# -- orders, fills, settlements, equity


def test_orders_and_fills_round_trip(store):
    o1, o2 = f.order("o-1", fills=2), f.order("o-2", episode_id="ep-2", fills=1)
    store.save_order(o1)
    store.save_order(o2)
    store.save_order(f.order("o-3", fills=0))
    assert store.list_orders(episode_id="ep-2") == [o2]
    assert [o.order_id for o in store.list_orders()] == ["o-1", "o-2", "o-3"]
    fills = store.list_fills(market_ticker=f.MARKET)
    assert fills == sorted(o1.fills + o2.fills, key=lambda x: x.filled_at)
    assert store.list_fills(market_ticker="OTHER") == []
    store.save_order(o1)  # saving again does not duplicate fills
    assert len(store.list_fills()) == 3


def test_settlements_and_equity_round_trip(store):
    store.record_settlement("M-2", Side.NO, f.at(2))
    store.record_settlement("M-1", Side.YES, f.at(1))
    assert store.list_settlements() == [("M-1", Side.YES, f.at(1)), ("M-2", Side.NO, f.at(2))]
    store.record_equity(f.at(1), Decimal("1000.00"))
    store.record_equity(f.at(0), Decimal("999.5"))
    series = store.equity_series()
    assert series == [(f.at(0), Decimal("999.5")), (f.at(1), Decimal("1000.00"))]
    assert all(isinstance(v, Decimal) for _, v in series)


# -- ledger


def test_resolve_market_writes_outcome_and_brier_scores(store):
    store.add_ledger_entries(
        [
            f.ledger_entry("e-1", 0.7, market_price=0.6),
            f.ledger_entry("e-2", 0.2, market_price=None),
            f.ledger_entry("e-3", 0.9, market="OTHER"),
        ]
    )
    assert store.resolve_market(f.MARKET, True, f.at(48)) == 2
    by_id = {e.entry_id: e for e in store.ledger()}
    assert by_id["e-1"].outcome is True and by_id["e-1"].resolved_at == f.at(48)
    assert by_id["e-1"].brier == pytest.approx(0.09)
    assert by_id["e-1"].market_brier == pytest.approx(0.16)
    assert by_id["e-2"].brier == pytest.approx(0.64)
    assert by_id["e-2"].market_brier is None
    assert by_id["e-3"].outcome is None
    # Already resolved entries are left alone.
    assert store.resolve_market(f.MARKET, False, f.at(50)) == 0
    assert store.ledger()[0].outcome is True
    assert store.resolve_market("OTHER", False, f.at(50)) == 1
    assert {e.entry_id: e for e in store.ledger()}["e-3"].brier == pytest.approx(0.81)


def ids(entries):
    return [e.entry_id for e in entries]


def test_ledger_filters(store):
    store.add_ledger_entries(
        [
            f.ledger_entry("e-1", 0.7, outcome=True, resolved_at=f.at(1)),
            f.ledger_entry("e-2", 0.7, outcome=True, resolved_at=f.at(5), category="cpi"),
            f.ledger_entry("e-3", 0.7, system="prompt_only", outcome=False, resolved_at=f.at(1)),
            f.ledger_entry("e-4", 0.7),
        ]
    )
    assert ids(store.ledger()) == ["e-1", "e-2", "e-3", "e-4"]
    assert ids(store.ledger(resolved_before=f.at(5))) == ["e-1", "e-3"]
    assert ids(store.ledger(category="cpi")) == ["e-2"]
    assert ids(store.ledger(system="prompt_only")) == ["e-3"]
    assert ids(store.ledger(resolved_before=f.at(6), category="fed", system="pipeline")) == ["e-1"]


def test_calibration_on_a_hand_computed_case(store):
    before = f.at(10)
    store.add_ledger_entries(
        [
            # Bin [0.1, 0.2): mean p 0.15, frequency 1/2 -> gap 0.35, weight 2/5.
            f.ledger_entry("a", 0.15, outcome=False, resolved_at=f.at(1)),
            f.ledger_entry("b", 0.15, outcome=True, resolved_at=f.at(2)),
            # Bin [0.8, 0.9): mean p 0.85, frequency 2/3 -> gap 0.18333, weight 3/5.
            f.ledger_entry("c", 0.82, outcome=True, resolved_at=f.at(3)),
            f.ledger_entry("d", 0.88, outcome=True, resolved_at=f.at(4)),
            f.ledger_entry("e", 0.85, outcome=False, resolved_at=f.at(5)),
            # Left out: another system, another category, resolved too late, unresolved.
            f.ledger_entry("x1", 0.99, outcome=False, resolved_at=f.at(1), system="prompt_only"),
            f.ledger_entry("x2", 0.99, outcome=False, resolved_at=f.at(1), category="cpi"),
            f.ledger_entry("x3", 0.99, outcome=False, resolved_at=f.at(10)),
            f.ledger_entry("x4", 0.99),
        ]
    )
    cal = store.calibration("fed", as_of=before)
    assert cal.category == "fed" and cal.n_resolved == 5
    assert cal.calibration_error == pytest.approx(0.4 * 0.35 + 0.6 * (0.85 - 2 / 3))
    assert cal.calibration_error == pytest.approx(0.25)
    empty = store.calibration("cpi", as_of=f.at(0))
    assert empty.n_resolved == 0 and empty.calibration_error is None


def test_resolved_entries_feed_calibration(store):
    store.add_ledger_entries([f.ledger_entry("e-1", 0.95)])
    store.resolve_market(f.MARKET, True, f.at(1))
    cal = store.calibration("fed", as_of=f.at(2))
    assert cal.n_resolved == 1 and cal.calibration_error == pytest.approx(0.05)
    assert store.calibration("fed", as_of=f.at(1)).n_resolved == 0  # strictly before as_of


# -- documents, halt, stage


def test_documents_remember_their_hash(store):
    assert store.document_hash("d-1") is None
    store.remember_document("d-1", "abc", f.at(0))
    store.remember_document("d-1", "def", f.at(1))
    assert store.document_hash("d-1") == "def"


def test_halt_and_stage_default_then_persist_across_reopening(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteStore(path) as s:
        assert s.get_halt() == HaltState(halted=False)
        assert s.get_stage() == StageState(stage=Stage.BACKTEST)
        s.set_halt("daily loss stop", f.at(1))
        s.set_stage(Stage.SHADOW, "benchmarks met", f.at(2))
    with SqliteStore(path) as s:
        assert s.get_halt() == HaltState(halted=True, reason="daily loss stop", at=f.at(1))
        assert s.get_stage() == StageState(stage=Stage.SHADOW, reason="benchmarks met", at=f.at(2))
        s.clear_halt()
    with SqliteStore(path) as s:
        assert s.get_halt().halted is False
        assert s.get_stage().stage is Stage.SHADOW


def test_file_database_uses_wal_and_records_its_schema_version(tmp_path):
    path = tmp_path / "state.sqlite"
    SqliteStore(path).close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaMismatch):
        SqliteStore(path)


def test_naive_times_are_refused(store):
    with pytest.raises(ValueError, match="naive"):
        store.calibration("fed", as_of=f.T0.replace(tzinfo=None))


def test_one_store_serves_many_threads(store):
    store.save_episode(f.episode())

    def work(i: int) -> None:
        for j in range(20):
            store.append_trace("ep-1", f.step(i * 100 + j))
            store.get_episode("ep-1")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.get_trace("ep-1")) == 80
