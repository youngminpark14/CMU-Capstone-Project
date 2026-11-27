"""The review queue: approve, resize (smaller only), reject (with a reason), and expiry."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pmfa.execution.review import ReviewQueue, ReviewRefused, build_card
from pmfa.records.decision import (
    ProposalStatus,
    ReasonCode,
    ReviewDecision,
    Routing,
    RoutingTier,
)
from pmfa.records.evidence import Passage
from pmfa.records.forecast import Forecast
from pmfa.records.market import Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.clocks import SimClock
from tests.world.recorded.factories import AS_OF, EVENT, HOLD, book, event

from .factories import CONFIG, proposal


def forecast() -> Forecast:
    return Forecast(
        forecast_id="fc-1",
        episode_id="ep-1",
        event_ticker=EVENT,
        as_of=AS_OF,
        outcome_probabilities={"hold": 0.7, "cut25": 0.3},
        market_probabilities={HOLD: 0.7},
        coverage=0.9,
        confidence=0.8,
        n_independent_origins=2,
    )


def passage(n: int, words: int = 10) -> Passage:
    return Passage(
        passage_id=f"doc-{n}#0",
        doc_id=f"doc-{n}",
        origin_id=f"doc-{n}",
        source="federalreserve.gov",
        url=f"https://www.federalreserve.gov/doc-{n}.htm",
        title=f"Document {n}",
        published_at=AS_OF - timedelta(days=n),
        index=0,
        text=" ".join(f"word{i}" for i in range(words)),
    )


def card():
    ev = event()
    return build_card(
        proposal(),
        ev,
        ev.market(HOLD),
        forecast(),
        Routing(tier=RoutingTier.APPROVE, score=1, reasons=["cost above one risk unit"]),
        [passage(1, 100), passage(2), passage(3), passage(4)],
    )


def queue() -> tuple[ReviewQueue, SqliteStore, SimClock]:
    store = SqliteStore(MEMORY)
    clock = SimClock(AS_OF + timedelta(minutes=1))
    q = ReviewQueue(store, clock, CONFIG)
    q.submit(proposal(), card())
    return q, store, clock


def test_card_shows_the_decision_inputs() -> None:
    c = card()
    assert c.event_title == "Fed decision in December 2026"
    assert c.market_title == "Fed maintains rate"
    assert (c.side, c.contracts, str(c.cost)) == (Side.YES, 10, "6.37")
    assert c.forecast_probability == pytest.approx(0.7)
    assert str(c.market_price) == "0.6200"
    assert [p.passage_id for p in c.passages] == ["doc-1#0", "doc-2#0", "doc-3#0"]
    assert c.passages[0].published_at == AS_OF - timedelta(days=1)
    assert c.passages[0].excerpt.endswith("…") and len(c.passages[0].excerpt.split()) == 41
    assert c.routing_reasons == ["cost above one risk unit"]


def test_card_for_a_no_proposal_shows_the_no_probability() -> None:
    ev = event()
    c = build_card(
        proposal(side=Side.NO),
        ev,
        ev.market(HOLD),
        forecast(),
        Routing(tier=RoutingTier.APPROVE),
        [],
    )
    assert c.forecast_probability == pytest.approx(0.3)


def test_submit_and_pending() -> None:
    q, _, _ = queue()
    assert [p.proposal_id for p in q.pending()] == ["prop-1"]
    _, status, stored_card = q.get("prop-1")
    assert status is ProposalStatus.PENDING
    assert stored_card is not None and stored_card.proposal_id == "prop-1"


def test_approve() -> None:
    q, store, clock = queue()
    review = q.decide("prop-1", ReviewDecision.APPROVE)
    assert review.decided_at == clock.now()
    assert q.get("prop-1")[1] is ProposalStatus.APPROVED
    assert store.list_reviews() == [review]
    assert q.pending() == []


def test_approve_takes_no_contract_count() -> None:
    q, _, _ = queue()
    with pytest.raises(ReviewRefused):
        q.decide("prop-1", ReviewDecision.APPROVE, contracts=5)


def test_resize_only_smaller() -> None:
    q, _, _ = queue()
    for bad in (None, 0, 10, 11):
        with pytest.raises(ReviewRefused):
            q.decide("prop-1", ReviewDecision.RESIZE, contracts=bad)
    review = q.decide(
        "prop-1", ReviewDecision.RESIZE, contracts=4, reason_code=ReasonCode.SIZE_TOO_LARGE
    )
    assert (review.contracts, review.reason_code) == (4, ReasonCode.SIZE_TOO_LARGE)
    assert q.get("prop-1")[1] is ProposalStatus.RESIZED


def test_reject_needs_a_reason_code() -> None:
    q, store, _ = queue()
    with pytest.raises(ReviewRefused):
        q.decide("prop-1", ReviewDecision.REJECT)
    assert store.list_reviews() == []
    review = q.decide(
        "prop-1", ReviewDecision.REJECT, reason_code=ReasonCode.STALE_EVIDENCE, note="old"
    )
    assert review.reason_code is ReasonCode.STALE_EVIDENCE
    assert q.get("prop-1")[1] is ProposalStatus.REJECTED


def test_an_already_decided_proposal_cannot_be_decided_again() -> None:
    q, store, _ = queue()
    q.decide("prop-1", ReviewDecision.APPROVE)
    with pytest.raises(ReviewRefused):
        q.decide("prop-1", ReviewDecision.REJECT, reason_code=ReasonCode.OTHER)
    assert len(store.list_reviews()) == 1
    assert q.get("prop-1")[1] is ProposalStatus.APPROVED


def test_expiry_by_time() -> None:
    q, store, clock = queue()
    p = proposal()
    clock.set(p.expires_at)
    assert not q.expired(p)
    clock.advance(1)
    assert q.expired(p)
    with pytest.raises(ReviewRefused):
        q.decide("prop-1", ReviewDecision.APPROVE)
    assert q.get("prop-1")[1] is ProposalStatus.EXPIRED
    assert store.list_reviews() == []


def test_expiry_by_price() -> None:
    q, _, _ = queue()
    p = proposal()  # edge bound 0.64
    at_bound = book(no_bids=(("0.36", "40"),))  # YES ask 0.64
    moved = book(no_bids=(("0.35", "40"),))  # YES ask 0.65
    empty = book(no_bids=())
    assert not q.expired(p, at_bound)
    assert not q.expired(p, empty)
    assert q.expired(p, moved)
    with pytest.raises(ReviewRefused):
        q.decide("prop-1", ReviewDecision.APPROVE, book=moved)
    assert q.get("prop-1")[1] is ProposalStatus.EXPIRED


def test_expire_and_expire_due() -> None:
    q, _, clock = queue()
    q.submit(proposal(proposal_id="prop-2", expires_at=AS_OF + timedelta(hours=1)), card())
    q.submit(proposal(proposal_id="prop-3", expires_at=AS_OF + timedelta(hours=1)), card())
    clock.set(AS_OF + timedelta(minutes=30))
    moved = book(no_bids=(("0.35", "40"),))
    calm = book()
    due = q.expire_due(lambda ticker: calm)
    assert [p.proposal_id for p in due] == ["prop-1"]
    q.expire("prop-2")
    assert q.get("prop-2")[1] is ProposalStatus.EXPIRED
    with pytest.raises(ReviewRefused):
        q.expire("prop-2")
    assert [p.proposal_id for p in q.expire_due(lambda ticker: moved)] == ["prop-3"]
    assert q.pending() == []


def test_a_proposal_whose_order_outcome_is_unknown_cannot_be_decided_or_expired() -> None:
    """Its order may stand on the exchange: no review or expiry may close it, even late."""
    q, store, clock = queue()
    store.set_proposal_status("prop-1", ProposalStatus.OUTCOME_UNKNOWN)
    clock.set(proposal().expires_at + timedelta(minutes=5))
    assert q.pending() == []
    for decision, extra in (
        (ReviewDecision.APPROVE, {}),
        (ReviewDecision.RESIZE, {"contracts": 1}),
        (ReviewDecision.REJECT, {"reason_code": ReasonCode.OTHER}),
    ):
        with pytest.raises(ReviewRefused, match="outcome_unknown"):
            q.decide("prop-1", decision, **extra)
    with pytest.raises(ReviewRefused, match="outcome_unknown"):
        q.expire("prop-1")
    assert q.expire_due() == []
    assert store.list_reviews() == []
    assert q.get("prop-1")[1] is ProposalStatus.OUTCOME_UNKNOWN
