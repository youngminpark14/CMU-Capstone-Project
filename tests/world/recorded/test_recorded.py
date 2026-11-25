"""The recorded world: its file format, the as-of rule in its ports, and the built world."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmfa.errors import ConfigError, NotFound
from pmfa.ports import Clock, Evidence, Exchange, Executor, Knowledge, Models, World
from pmfa.records.base import RunMode, Stage
from pmfa.records.decision import ReviewDecision, ReviewPolicy
from pmfa.records.market import Candle, MarketStatus, Side
from pmfa.state.store import SqliteStore
from pmfa.world.clocks import SimClock
from pmfa.world.recorded import (
    RecordedBooks,
    RecordedEvidence,
    RecordedExchange,
    RecordedWorld,
    ScriptedReview,
    build_recorded_world,
    load_recorded_world,
    save_recorded_world,
)

from .factories import (
    AS_OF,
    CANARY,
    CUT,
    EVENT,
    FUTURE,
    HOLD,
    MINUTES,
    SERIES,
    SPEECH,
    NoModels,
    book,
    event,
    prior_fill,
    resolved_entry,
    world_file,
)


def full_file():
    return world_file(
        execution_books={HOLD: book(no_bids=(("0.35", "100"),))},
        liquidity={CUT: {Side.YES: Decimal("750")}},
        candles={
            HOLD: [
                Candle(ticker=HOLD, end_ts=AS_OF - timedelta(hours=1), interval_minutes=60),
                Candle(ticker=HOLD, end_ts=AS_OF + timedelta(hours=1), interval_minutes=60),
            ]
        },
        ledger=[resolved_entry()],
        fills=[prior_fill()],
        bankroll="2000",
        halted=True,
        config_overrides={"caps": {"max_contracts_per_order": 10}},
        review=ScriptedReview(decision=ReviewDecision.RESIZE, contracts=3, after_seconds=120),
    )


# -- the file


def test_file_round_trips_through_json(tmp_path: Path) -> None:
    original = full_file()
    path = save_recorded_world(original, tmp_path / "world.json")
    loaded = load_recorded_world(path)
    assert loaded == original
    # Stable: saving what was loaded writes the same bytes.
    again = save_recorded_world(loaded, tmp_path / "again.json")
    assert again.read_text() == path.read_text()
    assert '"provenance": "hand-made fixture"' in path.read_text()


def test_scripted_review_becomes_a_review() -> None:
    scripted = ScriptedReview(decision=ReviewDecision.APPROVE, after_seconds=30)
    review = scripted.review_for("prop-1", AS_OF + timedelta(seconds=30))
    assert review.proposal_id == "prop-1"
    assert review.decision is ReviewDecision.APPROVE
    assert review.decided_at == AS_OF + timedelta(seconds=30)


# -- the exchange


def test_exchange_reads_the_recorded_markets_and_books() -> None:
    file = full_file()
    exchange = RecordedExchange(file, SimClock(AS_OF))
    assert exchange.get_series(SERIES).fee_type == "quadratic_with_maker_fees"
    assert [e.event_ticker for e in exchange.list_events(SERIES)] == [EVENT]
    assert exchange.get_market(HOLD).yes_ask == Decimal("0.62")
    assert exchange.get_order_book(HOLD).best_ask(Side.YES).price == Decimal("0.62")
    # CUT has no recorded book: one level per side from its quote.
    synthesized = exchange.get_order_book(CUT)
    assert synthesized.best_ask(Side.YES).price == Decimal("0.38")
    assert synthesized.best_bid(Side.YES).price == Decimal("0.36")
    with pytest.raises(NotFound):
        exchange.get_market("NOPE")


def test_liquidity_uses_the_override_else_the_book_within_the_band() -> None:
    exchange = RecordedExchange(full_file(), SimClock(AS_OF))
    assert exchange.liquidity_dollars(CUT, Side.YES) == Decimal("750")
    # YES asks 0.62 x 30 and 0.64 x 40, both within 2 cents of the touch.
    assert exchange.liquidity_dollars(HOLD, Side.YES) == Decimal("0.62") * 30 + Decimal("0.64") * 40


def test_exchange_answers_as_of_its_clock() -> None:
    settled = datetime(2026, 12, 9, 19, 30, tzinfo=UTC)
    file = world_file(
        events=[event(result="yes", settled_at=settled, status=MarketStatus.SETTLED)],
        candles={
            HOLD: [
                Candle(ticker=HOLD, end_ts=AS_OF - timedelta(hours=1), interval_minutes=60),
                Candle(ticker=HOLD, end_ts=AS_OF + timedelta(hours=1), interval_minutes=60),
            ]
        },
    )
    clock = SimClock(AS_OF)
    exchange = RecordedExchange(file, clock)
    hold = exchange.get_market(HOLD)
    assert hold.result is None and hold.settled_at is None
    candles = exchange.get_candles(
        HOLD, start=AS_OF - timedelta(days=1), end=AS_OF + timedelta(days=1), interval_minutes=60
    )
    assert [c.end_ts for c in candles] == [AS_OF - timedelta(hours=1)]
    clock.set(settled + timedelta(minutes=1))
    assert exchange.get_market(HOLD).result is Side.YES


def test_an_event_that_opens_after_the_clock_does_not_exist_yet() -> None:
    file = world_file()
    clock = SimClock(datetime(2026, 10, 1, tzinfo=UTC))
    exchange = RecordedExchange(file, clock)
    assert exchange.list_events(SERIES) == []
    with pytest.raises(NotFound):
        exchange.get_event(EVENT)


def test_books_use_the_execution_book_when_recorded() -> None:
    books = RecordedBooks(full_file())
    assert books(HOLD).best_ask(Side.YES).price == Decimal("0.65")
    assert books.book(CUT).best_ask(Side.YES).price == Decimal("0.38")


# -- evidence


def test_evidence_never_returns_a_document_after_the_as_of_time() -> None:
    evidence = RecordedEvidence(world_file())
    docs = evidence.documents(as_of=AS_OF)
    assert [d.doc_id for d in docs] == [SPEECH.doc_id, MINUTES.doc_id]
    assert FUTURE.doc_id not in {d.doc_id for d in docs}
    after = evidence.documents(as_of=AS_OF, published_after=AS_OF - timedelta(days=1))
    assert [d.doc_id for d in after] == [MINUTES.doc_id]
    assert evidence.get_document(MINUTES.doc_id, as_of=AS_OF) == MINUTES
    with pytest.raises(NotFound):
        evidence.get_document(FUTURE.doc_id, as_of=AS_OF)
    assert evidence.get_document(FUTURE.doc_id, as_of=AS_OF + timedelta(days=2)) == FUTURE


def test_evidence_calendar_window() -> None:
    evidence = RecordedEvidence(world_file())
    assert len(evidence.scheduled_events(start=AS_OF, end=AS_OF + timedelta(days=30))) == 1
    assert evidence.scheduled_events(start=AS_OF, end=AS_OF + timedelta(days=5)) == []


# -- the built world


def test_built_world_satisfies_the_ports() -> None:
    world = build_recorded_world(world_file(), models=NoModels(), review_policy=ReviewPolicy.PAUSE)
    assert isinstance(world, World) and isinstance(world, RecordedWorld)
    assert world.mode is RunMode.REPLAY
    assert isinstance(world.clock, Clock) and isinstance(world.clock, SimClock)
    assert world.clock.now() == AS_OF
    assert isinstance(world.exchange, Exchange)
    assert isinstance(world.executor, Executor)
    assert world.executor.simulated is True
    assert isinstance(world.evidence, Evidence)
    assert isinstance(world.knowledge, Knowledge)
    assert isinstance(world.models, Models)
    assert isinstance(world.store, SqliteStore)
    world.clock.advance(60)
    assert world.executor.execution_book(HOLD).as_of == AS_OF


def test_built_world_seeds_the_store() -> None:
    world = build_recorded_world(full_file(), models=NoModels(), review_policy=ReviewPolicy.PAUSE)
    store = world.store
    assert store.get_stage().stage is Stage.APPROVAL
    assert store.get_halt().halted
    assert [e.entry_id for e in store.ledger()] == ["ledger-oct-hold"]
    assert [f.fill_id for f in store.list_fills()] == ["prior-fill-1"]
    assert world.config.caps.max_contracts_per_order == 10
    # Bankroll 2000, minus 5 x 0.60 + 0.09 for the prior NO fill; equity keeps the cost.
    balance = world.executor.balance()
    assert balance.cash == Decimal("2000") - Decimal("3.09")
    assert balance.equity == Decimal("2000") - Decimal("0.09")
    assert store.equity_series() == [(AS_OF, balance.equity)]
    [position] = world.executor.positions(EVENT)
    assert (position.market_ticker, position.side, position.contracts) == (CUT, Side.NO, 5)


def test_knowledge_never_retrieves_the_future_document() -> None:
    world = build_recorded_world(world_file(), models=NoModels(), review_policy=ReviewPolicy.PAUSE)
    hits = world.knowledge.search_evidence(
        [CANARY, "Committee held the federal funds rate steady in December"],
        as_of=AS_OF,
        category="fed_decision",
        k=8,
    )
    assert hits, "the documents before the trigger should be retrievable"
    assert all(h.passage.doc_id != FUTURE.doc_id for h in hits)
    assert all(CANARY not in h.passage.text for h in hits)
    assert world.knowledge.get_passages([f"{FUTURE.doc_id}#0"], as_of=AS_OF) == []
    # It was indexed: the as-of filter, not omission, keeps it out.
    later = world.knowledge.get_passages([f"{FUTURE.doc_id}#0"], as_of=AS_OF + timedelta(days=2))
    assert [p.doc_id for p in later] == [FUTURE.doc_id]


def test_knowledge_holds_events_and_resolved_ledger() -> None:
    world = build_recorded_world(full_file(), models=NoModels(), review_policy=ReviewPolicy.PAUSE)
    hits = world.knowledge.search_events("Fed decision December hold", as_of=AS_OF)
    assert [h.event_ticker for h in hits] == [EVENT]
    ledger = world.knowledge.search_ledger(
        "Will the Fed hold rates?", as_of=AS_OF, category="fed_decision"
    )
    assert [h.question for h in ledger] == ["Will the Fed hold rates in October 2026?"]


def test_bad_overrides_are_a_config_error() -> None:
    with pytest.raises(ConfigError):
        build_recorded_world(
            world_file(config_overrides={"caps": {"no_such_cap": 1}}),
            models=NoModels(),
            review_policy=ReviewPolicy.PAUSE,
        )
