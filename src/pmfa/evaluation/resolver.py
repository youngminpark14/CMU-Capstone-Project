"""The outcome resolver: write settled results back to the ledger, the paper account, and the traces.

``resolve_outcomes(world, now=None)`` looks at every market that has an
unresolved ledger entry or an open paper holding. For each one whose result
the Exchange port now shows (as of the world's clock), it

1. writes the outcome and the Brier scores onto the market's ledger entries
   (``StateStore.resolve_market``);
2. settles the paper holding (``PaperExecutor.settle``, when the executor has it);
3. adds the newly resolved entries to the ledger index (``index_ledger``, when
   the Knowledge implementation has it), so later critics see them as
   calibration examples, never before their resolution time;
4. computes the pruning audit of every affected episode that grew a scenario
   tree, once the event's outcome is determined, and appends it to that
   episode's trace as a ``resolution`` observation.

Backtests call it before each news trigger and at the end; the live modes call
the same function. It is idempotent: a second call with nothing new settled
changes nothing.
"""

from __future__ import annotations

from datetime import datetime

from pmfa.errors import NotFound
from pmfa.forecast import OutcomeSpaceError, build_outcome_space, outcome_from_results
from pmfa.forecast.audit import PruningAudit, pruning_audit
from pmfa.ports import World
from pmfa.records.base import Record, UtcTime
from pmfa.records.episode import LedgerEntry, TraceStep
from pmfa.records.market import Market, Side

RESOLUTION_NODE = "resolution"


class EpisodeAudit(Record):
    """The pruning audit of one episode, once its event resolved."""

    episode_id: str
    event_ticker: str
    as_of: UtcTime
    audit: PruningAudit


class ResolutionReport(Record):
    """What one call of the resolver changed."""

    at: UtcTime
    markets_resolved: list[str] = []
    """Markets whose ledger entries got an outcome in this call."""
    entries_resolved: int = 0
    markets_settled: list[str] = []
    """Markets whose paper holding was settled in this call."""
    entries_indexed: int = 0
    audits: list[EpisodeAudit] = []
    unresolved: list[str] = []
    """Markets still waiting for a result."""


def _settled(market: Market) -> bool:
    return market.result is not None


def resolve_outcomes(world: World, *, now: datetime | None = None) -> ResolutionReport:
    """Resolve everything the exchange now shows as settled. See the module docstring."""
    at = now if now is not None else world.clock.now()
    store = world.store
    open_entries = [e for e in store.ledger() if e.outcome is None]
    settled_before = {ticker for ticker, _, _ in store.list_settlements()}
    held = {
        p.market_ticker for p in world.executor.positions() if p.market_ticker not in settled_before
    }
    candidates = sorted({e.market_ticker for e in open_entries} | held)

    resolved: list[str] = []
    settled: list[str] = []
    unresolved: list[str] = []
    entries_resolved = 0
    results: dict[str, Market] = {}
    for ticker in candidates:
        try:
            market = world.exchange.get_market(ticker)
        except NotFound:
            unresolved.append(ticker)
            continue
        if not _settled(market):
            unresolved.append(ticker)
            continue
        assert market.result is not None
        results[ticker] = market
        resolved_at = market.settled_at or at
        changed = store.resolve_market(ticker, market.result is Side.YES, resolved_at)
        if changed:
            resolved.append(ticker)
            entries_resolved += changed
        settle = getattr(world.executor, "settle", None)
        if ticker in held and settle is not None:
            settle(ticker, market.result, resolved_at)
            settled.append(ticker)

    newly = [
        e
        for e in store.ledger()
        if e.market_ticker in results
        and e.outcome is not None
        and e.entry_id in {o.entry_id for o in open_entries}
    ]
    indexed = 0
    index_ledger = getattr(world.knowledge, "index_ledger", None)
    if newly and index_ledger is not None:
        indexed = index_ledger(newly)

    audits = _audits(world, [e for e in store.ledger() if e.outcome is not None], at)
    return ResolutionReport(
        at=at,
        markets_resolved=resolved,
        entries_resolved=entries_resolved,
        markets_settled=settled,
        entries_indexed=indexed,
        audits=audits,
        unresolved=unresolved,
    )


def _audits(world: World, resolved: list[LedgerEntry], at: datetime) -> list[EpisodeAudit]:
    """Pruning audits for episodes with resolved entries and no audit yet, appended to their traces."""
    store = world.store
    out: list[EpisodeAudit] = []
    for episode_id in dict.fromkeys(e.episode_id for e in resolved):
        episode = store.get_episode(episode_id)
        if episode is None or episode.event_ticker is None:
            continue
        trace = store.get_trace(episode_id)
        if any(step.node == RESOLUTION_NODE for step in trace):
            continue
        nodes = store.get_nodes(episode_id)
        if not nodes:
            continue
        try:
            event = world.exchange.get_event(episode.event_ticker)
        except NotFound:
            continue
        sc = world.config.follow.for_ticker(event.series_ticker)
        if sc is None:
            continue
        # The tree was built on the markets listed at the episode's as-of time;
        # a ladder strike added later would change the bins, so it is left out.
        listed = [m for m in event.markets if m.open_time <= episode.as_of]
        if not listed or not all(_settled(m) for m in listed):
            continue
        try:
            space = build_outcome_space(event.model_copy(update={"markets": listed}), sc)
        except OutcomeSpaceError:
            continue
        realized = outcome_from_results(
            space, {m.ticker: m.result for m in listed if m.result is not None}
        )
        if realized is None:
            continue
        audit = pruning_audit(nodes, space, realized)
        store.append_trace(
            episode_id,
            TraceStep(
                seq=len(trace) + 1,
                node=RESOLUTION_NODE,
                kind="observation",
                summary=(
                    f"resolved {realized}: {audit.frozen_posterior_mass:.2f} of the posterior on "
                    f"frozen scenarios ({audit.n_frozen} of {audit.n_scenarios} frozen); most "
                    f"likely scenario {'was' if audit.most_likely_was_frozen else 'was not'} frozen"
                ),
                data={"pruning_audit": audit.model_dump(mode="json")},
                at=at,
            ),
        )
        out.append(
            EpisodeAudit(
                episode_id=episode_id,
                event_ticker=episode.event_ticker,
                as_of=episode.as_of,
                audit=audit,
            )
        )
    return out


__all__ = ["RESOLUTION_NODE", "EpisodeAudit", "ResolutionReport", "resolve_outcomes"]
