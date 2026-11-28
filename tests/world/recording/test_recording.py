"""Record and replay: an episode run through ``Recording`` replays exactly from what it saved."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmfa.examples import replay, review_policy, run_on_world
from pmfa.models.cassette import ModelTap
from pmfa.records.base import RunMode
from pmfa.records.decision import ReviewDecision
from pmfa.records.episode import EpisodeRecord, Exit
from pmfa.records.evidence import EventHit, Passage
from pmfa.records.market import OrderBook, Side
from pmfa.world.recorded import (
    RecordedKnowledge,
    RecordedKnowledgeCall,
    RecordedKnowledgeMiss,
    ScriptedReview,
    build_recorded_world,
    load_recorded_world,
    recorded_config,
)
from pmfa.world.recording import Recording, config_overrides, save_example
from tests.world.recorded.factories import filled_order

from .factories import AS_OF, CANARY, CUT, DEC, JAN, MINUTES, UNRELATED, UNUSED_ENTRY, scripted
from .factories import world_file as make_file


def record(source, mode=RunMode.REPLAY):
    """Run the source world's trigger through a recording; return file, cassette, run."""
    config = recorded_config(source)
    tape = ModelTap(scripted(config), config)
    world = build_recorded_world(
        source, models=tape, review_policy=review_policy(source), mode=mode
    )
    recording = Recording(world)
    run = run_on_world(recording.world, source)
    file = recording.file(source.trigger, description=source.description).model_copy(
        update={"review": source.review}
    )
    return file, tape.cassette(run.episode.episode_id), run


def decided(record: EpisodeRecord) -> dict:
    return {
        "exit": record.exit,
        "forecast": record.forecast,
        "proposal": record.proposal,
        "gate": record.gate,
        "routing": record.routing,
        "review": record.review,
        "orders": record.orders,
        "summaries": [(s.node, s.kind, s.summary) for s in record.trace],
    }


def without_call_source(record: EpisodeRecord) -> dict:
    """The whole record as JSON, minus each model call's answer source (scripted vs cassette)."""
    data = record.model_dump(mode="json")
    for step in data["trace"]:
        if step["call"] is not None:
            step["call"].pop("source")
    return data


@pytest.mark.parametrize(
    "review",
    [None, ScriptedReview(decision=ReviewDecision.APPROVE, after_seconds=30)],
    ids=["fill-tagged", "paused-and-approved"],
)
def test_a_recorded_episode_replays_identically_from_its_saved_files(tmp_path, review):
    source = make_file(review=review)
    file, cassette, run = record(source)
    assert run.episode.exit is Exit.EXECUTED
    assert run.episode.proposal is not None and run.episode.proposal.market_ticker == CUT

    save_example(tmp_path / "example", file, cassette, run.episode, run.card)
    result = replay(tmp_path / "example")

    assert decided(result.episode) == decided(run.episode)
    assert without_call_source(result.episode) == without_call_source(run.episode)
    assert result.card == run.card
    assert result.unplayed_calls == 0 and result.unplayed_knowledge == 0
    # The replay ran on recorded knowledge, not on a rebuilt index.
    assert load_recorded_world(tmp_path / "example" / "world.json").knowledge_calls


def test_the_recorded_file_holds_only_what_the_episode_read(tmp_path):
    file, _, run = record(make_file())
    doc_ids = {d.doc_id for d in file.documents}
    assert MINUTES.doc_id in doc_ids
    assert UNRELATED.doc_id not in doc_ids and CANARY.doc_id not in doc_ids
    # January was read as a match candidate, so it is recorded; its books were never read.
    assert [e.event_ticker for e in file.events] == [DEC, JAN]
    assert all(t.startswith(DEC) for t in file.order_books)
    assert UNUSED_ENTRY.scheduled_event_id not in {
        s.scheduled_event_id for s in file.scheduled_events
    }
    assert CUT in file.execution_books and set(file.liquidity[CUT]) == set(Side)
    methods = [c.method for c in file.knowledge_calls]
    assert methods[0] == "search_events" and "search_evidence" in methods and "origins" in methods
    # Retrieval results never include a passage published after the as-of time.
    for call in file.knowledge_calls:
        if call.method == "search_evidence":
            assert all(r["passage"]["doc_id"] != CANARY.doc_id for r in call.results)
    assert file.bankroll == Decimal("1000") and file.stage.value == "approval"
    assert file.config_overrides == {"state": {"transport": "direct"}}
    assert file.provenance == "recorded"
    assert run.episode.passage_ids


def test_a_price_move_before_execution_is_recorded_and_replayed(tmp_path):
    from pmfa.records.market import BookLevel

    # Buying NO on the cut market now meets 0.73: past the edge bound, so no order is sent.
    moved = OrderBook(
        ticker=CUT,
        as_of=AS_OF,
        yes_bids=[BookLevel(price="0.27", contracts="1000")],
        no_bids=[BookLevel(price="0.66", contracts="1000")],
    )
    file, cassette, run = record(make_file(execution_books={CUT: moved}))
    assert run.episode.orders == []
    assert file.execution_books[CUT] == moved
    save_example(tmp_path / "moved", file, cassette, run.episode, run.card)
    result = replay(tmp_path / "moved")
    assert decided(result.episode) == decided(run.episode)


def test_recorded_knowledge_serves_calls_in_order_and_refuses_anything_else():
    at = datetime(2026, 11, 20, tzinfo=UTC)
    hit = EventHit(event_ticker=DEC, similarity=0.5)
    calls = [
        RecordedKnowledgeCall(
            method="search_events",
            arguments={"query": "fed", "as_of": at.isoformat(), "k": 5},
            results=[hit.model_dump(mode="json")],
        ),
        RecordedKnowledgeCall(
            method="origins",
            arguments={"passage_ids": ["d#0"], "category": "fed_decision"},
            results=[],
        ),
    ]
    knowledge = RecordedKnowledge(calls)
    assert knowledge.search_events("fed", as_of=at, k=5) == [hit]
    passage = Passage(
        passage_id="d#0",
        doc_id="d",
        origin_id="d",
        source="federalreserve.gov",
        url="https://federalreserve.gov/d",
        title="d",
        published_at=at,
        index=0,
        text="text",
    )
    with pytest.raises(RecordedKnowledgeMiss, match="category"):
        knowledge.origins([passage], category="cpi")
    assert knowledge.origins([passage], category="fed_decision") == []
    with pytest.raises(RecordedKnowledgeMiss, match="not recorded"):
        knowledge.search_events("fed", as_of=at)

    other = RecordedKnowledge(calls)
    with pytest.raises(RecordedKnowledgeMiss, match="asks for search_ledger"):
        other.search_ledger("fed", as_of=at, category="fed_decision")
    with pytest.raises(RecordedKnowledgeMiss, match="query"):
        other.search_events("a different query", as_of=at, k=5)


def test_build_recorded_world_uses_recorded_knowledge_only_when_the_file_has_calls():
    from pmfa.knowledge.retriever import ChromaKnowledge

    plain = make_file()
    config = recorded_config(plain)
    world = build_recorded_world(plain, models=scripted(config), review_policy=review_policy(plain))
    assert isinstance(world.knowledge, ChromaKnowledge)
    recorded, _, _ = record(plain)
    world = build_recorded_world(
        recorded, models=scripted(config), review_policy=review_policy(recorded)
    )
    assert isinstance(world.knowledge, RecordedKnowledge)


def test_config_overrides_are_the_differences_from_the_defaults():
    config = recorded_config(
        make_file(config_overrides={"forecast": {"max_depth": 1}, "state": {"transport": "direct"}})
    )
    assert config_overrides(config) == {
        "forecast": {"max_depth": 1},
        "state": {"transport": "direct"},
    }


def test_save_example_writes_the_layout_and_keeps_a_hand_written_readme(tmp_path):
    source = make_file(review=ScriptedReview(decision=ReviewDecision.APPROVE))
    file, cassette, run = record(source)
    folder = tmp_path / "ex"
    save_example(folder, file, cassette, run.episode, run.card)
    for name in ("world.json", "cassette.json", "expected/episode.json", "README.md"):
        assert (folder / name).is_file(), name
    assert (folder / "expected" / "review_card.json").is_file()
    (folder / "README.md").write_text("hand-written\n", encoding="utf-8")
    save_example(folder, file, cassette, run.episode, None)
    assert (folder / "README.md").read_text(encoding="utf-8") == "hand-written\n"
    assert not (folder / "expected" / "review_card.json").exists()


def test_an_episode_recorded_in_backtest_mode_replays_in_that_mode(tmp_path):
    file, cassette, run = record(make_file(), mode=RunMode.BACKTEST)
    assert file.mode is RunMode.BACKTEST and run.episode.mode is RunMode.BACKTEST
    save_example(tmp_path / "bt", file, cassette, run.episode, run.card)
    result = replay(tmp_path / "bt")
    assert result.episode.episode_id == run.episode.episode_id
    assert decided(result.episode) == decided(run.episode)


def test_a_world_holding_a_position_in_another_event_records_and_replays_identically(tmp_path):
    from pmfa.records.decision import Fill
    from pmfa.records.evidence import Trigger, TriggerKind

    held = f"{JAN}-H0"
    fill = Fill(
        fill_id="prior-fill-1",
        order_id="prior-order-1",
        market_ticker=held,
        side=Side.YES,
        contracts="5",
        price="0.62",
        fee="0.02",
        filled_at=AS_OF.replace(day=17),
        simulated=True,
    )
    # A schedule trigger names its event, so the episode itself never reads January.
    trigger = Trigger(trigger_id="sched-dec", kind=TriggerKind.SCHEDULE, at=AS_OF, event_ticker=DEC)
    file, cassette, run = record(make_file(orders=[filled_order([fill])], trigger=trigger))
    assert JAN in {e.event_ticker for e in file.events}
    assert [f for o in file.orders for f in o.fills] == [fill]
    save_example(tmp_path / "held", file, cassette, run.episode, run.card)
    result = replay(tmp_path / "held")
    assert decided(result.episode) == decided(run.episode)
    assert without_call_source(result.episode) == without_call_source(run.episode)


def test_a_recording_of_a_world_built_without_starting_history_writes_no_orders(tmp_path):
    import json

    from pmfa.world.recorded import save_recorded_world

    file, _, run = record(make_file())
    assert run.episode.orders
    # The only equity point is the one the built world wrote at the trigger time.
    assert file.orders == [] and file.settlements == []
    assert [p.at for p in file.equity] == [AS_OF]
    data = json.loads(save_recorded_world(file, tmp_path / "world.json").read_text())
    assert not {"orders", "settlements"} & set(data)


def test_a_recording_started_at_a_time_holds_nothing_of_the_store_dated_after_it():
    from datetime import timedelta

    from pmfa.records.decision import Fill, OrderResult, OrderStatus
    from pmfa.records.episode import LedgerEntry

    def fill(order_id: str, at: datetime, n: int = 0) -> Fill:
        return Fill(
            fill_id=f"{order_id}-fill-{n}",
            order_id=order_id,
            market_ticker=CUT,
            side=Side.NO,
            contracts="4",
            price="0.66",
            fee="0.07",
            filled_at=at,
            simulated=True,
        )

    def filled(
        order_id: str,
        placed_at: datetime,
        outcome_at: datetime | None,
        fills: list[Fill] | None = None,
    ) -> OrderResult:
        fills = fills or [fill(order_id, outcome_at or placed_at)]
        return OrderResult(
            order_id=order_id,
            client_order_id=order_id,
            episode_id=f"ep-{order_id}",
            market_ticker=CUT,
            side=Side.NO,
            requested_contracts=4 * len(fills),
            limit_price="0.66",
            status=OrderStatus.FILLED,
            fills=fills,
            placed_at=placed_at,
            outcome_at=outcome_at,
        )

    def entry(name: str, resolved_at: datetime | None) -> LedgerEntry:
        return LedgerEntry(
            entry_id=f"ledger-{name}",
            episode_id=f"ep-{name}",
            mode=RunMode.REPLAY,
            event_ticker=DEC,
            market_ticker=CUT,
            category="fed_decision",
            question="Will the Fed cut 25 bp in December?",
            as_of=AS_OF - timedelta(days=2) if resolved_at else AS_OF,
            probability=0.3,
            market_price=0.35,
            outcome=False if resolved_at else None,
            resolved_at=resolved_at,
            brier=0.09 if resolved_at else None,
            market_brier=0.1225 if resolved_at else None,
            traded=True,
        )

    source = make_file()
    config = recorded_config(source)
    world = build_recorded_world(
        source, models=ModelTap(scripted(config), config), review_policy=review_policy(source)
    )
    earlier = filled("earlier", AS_OF - timedelta(days=1), None)
    # Sent at the as-of time, but priced from a candle that ends two minutes later.
    pending = filled("pending", AS_OF, AS_OF + timedelta(minutes=2))
    # Its outcome is known an hour before, but the exchange reported a second fill later.
    hour_ago = AS_OF - timedelta(hours=1)
    split = filled(
        "split",
        hour_ago,
        None,
        [fill("split", hour_ago), fill("split", AS_OF + timedelta(minutes=1), 1)],
    )
    world.store.save_order(earlier)
    world.store.save_order(pending)
    world.store.save_order(split)
    world.store.record_settlement(CUT, Side.NO, AS_OF + timedelta(days=1))
    world.store.record_equity(AS_OF + timedelta(minutes=2), Decimal("990"))
    # Resolved before the as-of time, resolved after it, and not resolved yet (the order
    # behind its ``traded`` flag is the pending one).
    resolved = entry("resolved", AS_OF - timedelta(days=1))
    world.store.add_ledger_entries(
        [resolved, entry("later", AS_OF + timedelta(hours=1)), entry("pending", None)]
    )

    file = Recording(world).file(source.trigger, description=source.description)

    assert file.as_of == AS_OF
    assert [o.order_id for o in file.orders] == ["earlier", "split"]
    assert all((o.outcome_at or o.placed_at) <= AS_OF for o in file.orders)
    fills = [f for o in file.orders for f in o.fills]
    assert fills and all(f.filled_at <= AS_OF for f in fills)
    assert [f.fill_id for f in file.orders[1].fills] == ["split-fill-0"]
    assert file.settlements == []
    assert all(p.at <= AS_OF for p in file.equity)
    assert file.ledger == [resolved]

    # The account as of the as-of time is the same in the rebuilt world.
    rebuilt = build_recorded_world(
        file, models=ModelTap(scripted(config), config), review_policy=review_policy(file)
    )
    assert rebuilt.executor.balance() == world.executor.balance()
    assert rebuilt.executor.positions() == world.executor.positions()


def test_the_book_an_order_filled_against_is_recorded_and_replayed(tmp_path):
    from pmfa.records.decision import OrderStatus
    from pmfa.records.market import BookLevel

    # The decision reads the deep execution book; the order fills against a thin one
    # (as a backtest's candle after the as-of time would be), so it fills partly.
    thin = OrderBook(
        ticker=CUT,
        as_of=AS_OF,
        yes_bids=[BookLevel(price="0.32", contracts="3")],
        no_bids=[BookLevel(price="0.66", contracts="1000")],
    )
    file, cassette, run = record(make_file(fill_books={CUT: thin}))
    [order] = run.episode.orders
    assert order.status is OrderStatus.PARTIALLY_FILLED and order.filled_contracts == 3
    assert file.fill_books == {CUT: thin}
    assert file.execution_books[CUT] != thin
    save_example(tmp_path / "thin", file, cassette, run.episode, run.card)
    result = replay(tmp_path / "thin")
    assert decided(result.episode) == decided(run.episode)


def test_a_fill_book_equal_to_the_execution_book_is_not_recorded(tmp_path):
    file, _, run = record(make_file())
    assert run.episode.orders and file.fill_books == {}


class _NoLookup:
    """An executor written before ``find_order`` joined the Executor port."""

    simulated = True


class _LookupFails(_NoLookup):
    def find_order(self, intent):
        from pmfa.errors import ExchangeError

        raise ExchangeError("GET /portfolio/orders -> 503")


def _intent():
    from pmfa.records.decision import OrderIntent

    return OrderIntent(
        client_order_id="cid-1",
        episode_id="ep-1",
        proposal_id="prop-1",
        market_ticker=CUT,
        side=Side.YES,
        contracts=1,
        limit_price=Decimal("0.40"),
    )


def test_the_recording_executor_asks_the_inner_executor_and_never_guesses_not_found():
    from pmfa.errors import ExchangeError
    from pmfa.world.recording import _RecordingExecutor

    # None would read as "no such order: nothing was placed"; a missing method is a bug.
    with pytest.raises(AttributeError):
        _RecordingExecutor(None, _NoLookup()).find_order(_intent())  # type: ignore[arg-type]
    with pytest.raises(ExchangeError):
        _RecordingExecutor(None, _LookupFails()).find_order(_intent())  # type: ignore[arg-type]
