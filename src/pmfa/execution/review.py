"""The review queue: proposals that wait for a human, and the human's decision.

A proposal routed to the approve tier is saved ``PENDING`` with a review card.
The human approves, resizes (smaller only), or rejects it with a reason code.
A proposal expires when the clock passes its ``expires_at`` or when the best
ask on its side has moved above its edge-preserving bound; an expired or
already decided proposal cannot be decided. Approval does not place anything:
the episode re-runs the gate at the current price and places the order in a
separate step.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pmfa.config import Config
from pmfa.errors import NotFound, PmfaError
from pmfa.ports import Clock, StateStore
from pmfa.records.decision import (
    Proposal,
    ProposalStatus,
    ReasonCode,
    Review,
    ReviewCard,
    ReviewCardPassage,
    ReviewDecision,
    Routing,
)
from pmfa.records.evidence import Passage, RetrievedPassage
from pmfa.records.forecast import Forecast
from pmfa.records.market import Event, Market, OrderBook, Side

CARD_PASSAGES = 3
EXCERPT_WORDS = 40


class ReviewRefused(PmfaError, ValueError):
    """A review that the queue will not accept: wrong state, expired, or an invalid decision."""


def excerpt(text: str, words: int = EXCERPT_WORDS) -> str:
    """The first ``words`` words of ``text``, with an ellipsis when cut."""
    parts = text.split()
    cut = " ".join(parts[:words])
    return cut + " …" if len(parts) > words else cut


def build_card(
    proposal: Proposal,
    event: Event,
    market: Market,
    forecast: Forecast,
    routing: Routing,
    passages: Sequence[Passage | RetrievedPassage],
) -> ReviewCard:
    """The card a reviewer reads: event and market, forecast against price, size and cost,
    the first three passages (in the order given, so pass them ranked) with dates, and why
    the proposal was routed to a human.

    The forecast shown is the probability that the proposal's side wins, taken
    from the forecast for its market; the price is the best ask on that side when
    the proposal was sized.
    """
    p_yes = forecast.market_probabilities.get(proposal.market_ticker)
    if p_yes is None:
        p_side = proposal.forecast_probability
    else:
        p_side = p_yes if proposal.side is Side.YES else 1.0 - p_yes
    shown = []
    for item in list(passages)[:CARD_PASSAGES]:
        p = item.passage if isinstance(item, RetrievedPassage) else item
        shown.append(
            ReviewCardPassage(
                passage_id=p.passage_id,
                source=p.source,
                title=p.title,
                published_at=p.published_at,
                excerpt=excerpt(p.text),
            )
        )
    reasons = list(routing.reasons)
    if routing.would_be_auto:
        reasons.append("would be auto at the autonomous stage")
    return ReviewCard(
        proposal_id=proposal.proposal_id,
        episode_id=proposal.episode_id,
        event_title=event.title,
        market_title=market.subtitle or market.title,
        market_ticker=proposal.market_ticker,
        side=proposal.side,
        contracts=proposal.contracts,
        limit_price=proposal.limit_price,
        cost=proposal.cost,
        forecast_probability=p_side,
        market_price=proposal.reference_price,
        edge=proposal.edge,
        confidence=forecast.confidence,
        routing_reasons=reasons,
        passages=shown,
        expires_at=proposal.expires_at,
    )


class ReviewQueue:
    """Pending proposals in the state store, and the decisions made on them."""

    def __init__(self, store: StateStore, clock: Clock, config: Config) -> None:
        self.store = store
        self.clock = clock
        self.config = config

    def submit(self, proposal: Proposal, card: ReviewCard) -> None:
        self.store.save_proposal(proposal, ProposalStatus.PENDING, card)

    def pending(self) -> list[Proposal]:
        return [p for p, _ in self.store.list_proposals(ProposalStatus.PENDING)]

    def get(self, proposal_id: str) -> tuple[Proposal, ProposalStatus, ReviewCard | None]:
        found = self.store.get_proposal(proposal_id)
        if found is None:
            raise NotFound(f"no proposal {proposal_id!r}")
        return found

    def expired(self, proposal: Proposal, book: OrderBook | None = None) -> bool:
        """True once the clock is past ``expires_at``, or the best ask on the proposal's
        side in ``book`` is above its ``edge_bound_price``. A book with no offer on that
        side does not expire the proposal (the order would simply not fill)."""
        if self.clock.now() > proposal.expires_at:
            return True
        if book is None:
            return False
        best = book.best_ask(proposal.side)
        return best is not None and best.price > proposal.edge_bound_price

    def decide(
        self,
        proposal_id: str,
        decision: ReviewDecision,
        *,
        contracts: int | None = None,
        reason_code: ReasonCode | None = None,
        note: str = "",
        book: OrderBook | None = None,
        reviewer: str = "human",
    ) -> Review:
        """Validate and record a human decision, and set the proposal's status.

        Only a pending, unexpired proposal can be decided (``book``, when given,
        adds the price check to the time check; a proposal found expired is
        marked ``EXPIRED``). Approve takes nothing else; resize needs a contract
        count above zero and below the proposal's; reject needs a reason code.
        """
        decision = ReviewDecision(decision)
        proposal, status, _ = self.get(proposal_id)
        if status is not ProposalStatus.PENDING:
            raise ReviewRefused(f"proposal {proposal_id} is {status.value}, not pending")
        if self.expired(proposal, book):
            self.store.set_proposal_status(proposal_id, ProposalStatus.EXPIRED)
            raise ReviewRefused(f"proposal {proposal_id} has expired")
        if decision is ReviewDecision.APPROVE:
            if contracts is not None:
                raise ReviewRefused("approve takes no contract count; use resize to change size")
            new_status = ProposalStatus.APPROVED
        elif decision is ReviewDecision.RESIZE:
            if contracts is None or not (0 < contracts < proposal.contracts):
                raise ReviewRefused(
                    f"resize needs a contract count between 1 and {proposal.contracts - 1}: "
                    "a review can shrink a trade, never grow it"
                )
            new_status = ProposalStatus.RESIZED
        else:
            if reason_code is None:
                raise ReviewRefused("reject needs a reason code")
            if contracts is not None:
                raise ReviewRefused("reject takes no contract count")
            new_status = ProposalStatus.REJECTED
        review = Review(
            proposal_id=proposal_id,
            decision=decision,
            contracts=contracts,
            reason_code=ReasonCode(reason_code) if reason_code is not None else None,
            note=note,
            reviewer=reviewer,
            decided_at=self.clock.now(),
        )
        self.store.save_review(review)
        self.store.set_proposal_status(proposal_id, new_status)
        return review

    def expire(self, proposal_id: str) -> None:
        """Mark a pending proposal ``EXPIRED``."""
        _, status, _ = self.get(proposal_id)
        if status is not ProposalStatus.PENDING:
            raise ReviewRefused(f"proposal {proposal_id} is {status.value}, not pending")
        self.store.set_proposal_status(proposal_id, ProposalStatus.EXPIRED)

    def expire_due(self, book_for: Callable[[str], OrderBook] | None = None) -> list[Proposal]:
        """Expire every pending proposal past due; ``book_for(market_ticker)`` adds the price check."""
        expired = []
        for proposal in self.pending():
            book = book_for(proposal.market_ticker) if book_for is not None else None
            if self.expired(proposal, book):
                self.store.set_proposal_status(proposal.proposal_id, ProposalStatus.EXPIRED)
                expired.append(proposal)
        return expired


__all__ = ["ReviewQueue", "ReviewRefused", "build_card", "excerpt"]
