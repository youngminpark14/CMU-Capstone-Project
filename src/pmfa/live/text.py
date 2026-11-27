"""Plain-text lines for the operator commands: episodes, proposals, review cards, decisions.

Pure functions that return strings; the command line prints them.
"""

from __future__ import annotations

from datetime import datetime

from pmfa.records.base import RunMode
from pmfa.records.decision import OrderResult, Proposal, ProposalStatus, ReviewCard
from pmfa.records.episode import EpisodeRecord, EpisodeStatus, Exit

from .loop import EpisodeOutcome, PassSummary


def when(moment: datetime | None) -> str:
    return "-" if moment is None else f"{moment:%Y-%m-%d %H:%M} UTC"


def unknown_outcome_sentence(mode: RunMode) -> str:
    """What an order whose outcome is unknown means for the operator, in one sentence."""
    return (
        "Such an order may stand on the exchange: nothing more will be sent for it, the next "
        f"pass asks the exchange again, and pmfa reconcile --mode {mode.value} asks now."
    )


def proposal_line(p: Proposal) -> str:
    return (
        f"buy {p.contracts} {p.side.value.upper()} {p.market_ticker} at <= {p.limit_price} "
        f"(cost ${p.cost}, edge {p.edge:.4f})"
    )


def order_lines(orders: list[OrderResult]) -> list[str]:
    lines = []
    for o in orders:
        head = (
            f"order {o.order_id or '(none)'}: {o.status.value}, {o.filled_contracts} of "
            f"{o.requested_contracts} {o.side.value.upper()} {o.market_ticker}"
        )
        if o.error:
            head += f" ({o.error})"
        lines.append(head)
        lines.extend(
            f"  fill {f.contracts} at {f.price}, fee ${f.fee}"
            + (" (simulated)" if f.simulated else "")
            for f in o.fills
        )
    return lines


def episode_line(o: EpisodeOutcome) -> str:
    """One line per episode: trigger, event, exit (or the pause), proposal when there is one."""
    target = o.event_ticker or "no event"
    end = o.exit.value if o.exit is not None else o.status.value
    line = f"{o.trigger.kind.value} {o.trigger.trigger_id} -> {target}: {end}"
    if o.exit_detail:
        line += f" ({o.exit_detail})"
    if o.proposal is not None:
        line += f"; proposal {o.proposal.proposal_id}: {proposal_line(o.proposal)}"
    if o.already_run:
        line += " [already run]"
    return line


def pass_lines(s: PassSummary) -> list[str]:
    lines = [
        f"pass {when(s.started_at)}: {len(s.open_events)} open event(s) indexed"
        f" ({', '.join(s.open_events) or 'none'}); "
        + (
            f"first look: {s.passages_indexed} passage(s) indexed, no document triggers"
            if s.first_look
            else f"{s.documents_new} new document(s), {s.passages_indexed} passage(s) indexed"
        )
    ]
    if s.resolved:
        lines.append(f"resolved: {len(s.resolved)}")
        lines.extend(f"  {r}" for r in s.resolved)
    if s.expired:
        lines.append(f"expired proposals: {', '.join(s.expired)}")
    if s.halted:
        lines.append(f"HALTED: {s.halt_reason} (no order will be placed)")
    if not s.triggers:
        lines.append("no triggers")
    if s.reconciled:
        lines.append(f"order outcomes learned by asking again: {len(s.reconciled)}")
        lines.extend(f"  {r}" for r in s.reconciled)
    lines.extend(episode_line(o) for o in s.episodes)
    for o in s.episodes:
        lines.extend(f"  {line}" for line in order_lines(o.orders))
    if s.outcome_unknown:
        lines.append(
            f"order outcome unknown for proposal(s) {', '.join(s.outcome_unknown)}. "
            + unknown_outcome_sentence(s.mode)
        )
    if s.equity is not None:
        lines.append(f"equity ${s.equity}")
    lines.extend(f"note: {n}" for n in s.notes)
    if s.stopped:
        lines.append(f"stopped: {s.stopped}")
    return lines


def pending_line(p: Proposal, status: ProposalStatus, now: datetime) -> str:
    late = " (past expiry)" if now > p.expires_at else ""
    return (
        f"{p.proposal_id}  episode {p.episode_id}  {proposal_line(p)}  "
        f"expires {when(p.expires_at)}{late}  [{status.value}]"
    )


def card_lines(card: ReviewCard) -> list[str]:
    lines = [
        f"proposal {card.proposal_id} (episode {card.episode_id})",
        f"event:    {card.event_title}",
        f"market:   {card.market_title} ({card.market_ticker})",
        f"forecast: {card.forecast_probability:.3f} that {card.side.value.upper()} wins, "
        f"against a price of {card.market_price} (edge after fees {card.edge:.4f}, "
        f"confidence {card.confidence:.2f})",
        f"size:     buy {card.contracts} {card.side.value.upper()} at <= {card.limit_price}, "
        f"cost up to ${card.cost}",
        f"expires:  {when(card.expires_at)}",
        "routed to review because (auto conditions that did not hold, or the stage):",
    ]
    lines.extend(f"  - {r}" for r in card.routing_reasons or ["(no reason recorded)"])
    lines.append("top passages:")
    if not card.passages:
        lines.append("  (none)")
    for i, p in enumerate(card.passages, 1):
        lines.append(f"  {i}. [{p.published_at:%Y-%m-%d %H:%M} UTC] {p.source}: {p.title}")
        lines.append(f"     {p.excerpt}")
    return lines


def decision_lines(record: EpisodeRecord) -> list[str]:
    """What happened after a review: executed with fills, gate rejected, expired, ..."""
    exit = record.exit
    if exit is None and record.status is EpisodeStatus.OUTCOME_UNKNOWN:
        return [
            f"order outcome unknown for episode {record.episode_id}. "
            + unknown_outcome_sentence(record.mode)
        ]
    if exit is None:
        return [f"episode {record.episode_id} is {record.status.value}"]
    if exit is Exit.EXECUTED:
        return ["executed: " + record.exit_detail, *order_lines(record.orders)]
    if exit is Exit.GATE_REJECTED:
        reasons = ", ".join(record.gate.reasons) if record.gate else record.exit_detail
        return [f"gate rejected at the current price: {reasons}; no order sent"]
    if exit is Exit.EXPIRED:
        return [f"expired: {record.exit_detail}; no order sent"]
    if exit is Exit.REJECTED_BY_HUMAN:
        return [f"rejected: {record.exit_detail}; no order sent"]
    return [f"{exit.value}: {record.exit_detail}"]


__all__ = [
    "card_lines",
    "decision_lines",
    "episode_line",
    "order_lines",
    "pass_lines",
    "pending_line",
    "proposal_line",
    "unknown_outcome_sentence",
    "when",
]
