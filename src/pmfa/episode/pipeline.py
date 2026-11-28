"""The episode pipeline's steps, one method per graph node.

``Pipeline`` closes over the ``World`` (the ports) and the state client. Graph
state holds plain JSON only (ids, and records dumped with
``model_dump(mode="json")``), so a paused episode survives a checkpoint written
in one process and read in another. Every node returns a state update whose
``goto`` names the next node; a node that ends the episode sets ``exit`` and
goes to ``record``, which runs last on every path. An episode whose order's
outcome is unknown has no exit yet: it waits in ``hold`` and ``reconcile``
until a lookup by its client order id says what the exchange did.

Shared state of the roles (tree nodes, beams, rejections) goes through the
``StateClient``. Everything else (episodes, traces, forecasts, proposals,
orders, the ledger) goes to the store directly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, TypedDict

from pmfa.config import SeriesConfig
from pmfa.decision import (
    GateContext,
    RoutingContext,
    SizingInputs,
    gate,
    origin_rule_met,
    risk_state,
    route,
    size_event,
    size_fn,
)
from pmfa.errors import (
    AsOfViolation,
    BudgetExhausted,
    CassetteMiss,
    ExchangeError,
    ModelOutputError,
    NotFound,
    PmfaError,
    RunBudgetExhausted,
)
from pmfa.forecast import (
    EvidenceBatch,
    ForecastEngine,
    ForecastRequest,
    OutcomeSpaceError,
    SearchState,
    build_outcome_space,
    event_brief,
    is_fast_path,
    levels,
)
from pmfa.forecast.tree import passage_brief
from pmfa.models.meter import add_usage
from pmfa.ports import World
from pmfa.records.base import RunMode, Stage, stable_id
from pmfa.records.decision import (
    GateResult,
    OrderIntent,
    OrderResult,
    OrderStatus,
    Proposal,
    ProposalStatus,
    Review,
    ReviewDecision,
    ReviewPolicy,
    Routing,
    RoutingTier,
)
from pmfa.records.episode import (
    EpisodeRecord,
    EpisodeStatus,
    Exit,
    LedgerEntry,
    RiskState,
    Usage,
)
from pmfa.records.evidence import (
    Document,
    Origin,
    RetrievedPassage,
    Trigger,
    TriggerKind,
)
from pmfa.records.forecast import Forecast
from pmfa.records.market import Event, MarketStatus, OrderBook, Side
from pmfa.records.roles import (
    EventBrief,
    LedgerExample,
    MatchDecision,
    MatchInput,
    PassageSelection,
    PassageSelectionInput,
    QueryPlan,
    QueryPlanInput,
    RejectionBrief,
    RoleTask,
    TriggerBrief,
)
from pmfa.state.client import StateClient

from .trace import Tracer, TracingModels, dump

EXCERPT_WORDS = 120
"""How much of a trigger document the Monitor and the Researcher see."""


class EpisodeState(TypedDict, total=False):
    """Graph state: plain JSON values only, so it survives a checkpoint in another process."""

    episode_id: str
    mode: str
    trigger: dict[str, Any]
    trigger_brief: dict[str, Any] | None
    as_of: str
    started_at: str
    goto: str
    status: str
    exit: str | None
    exit_detail: str
    excluded: list[str]
    """Events a re-match may not pick again."""
    rematched: bool
    event_ticker: str | None
    retrieved: list[dict[str, Any]] | None
    """The kept passages, as ``RetrievedPassage``, in rank order."""
    origins: list[dict[str, Any]] | None
    search: dict[str, Any] | None
    """The forecast engine's ``SearchState`` between beam steps."""
    sizing: dict[str, Any] | None
    """``SizingInputs`` for the current proposal: books, holdings, bankroll. Read when the
    forecast started, and read again (execution books) by the one recompute."""
    forecast_mids: dict[str, str | None] | None
    """Each market's YES mid when the forecast started, as a decimal string. Nothing
    overwrites it, the recompute included: it is the ledger's price at forecast time and
    the base of routing's price-move check."""
    refresh_books: bool
    recomputed: bool
    forecast: dict[str, Any] | None
    proposal: dict[str, Any] | None
    gate: dict[str, Any] | None
    routing: dict[str, Any] | None
    card: dict[str, Any] | None
    answer: dict[str, Any] | None
    """What the review command resumed the graph with."""
    review: dict[str, Any] | None
    fill_tagged: bool
    run_budget_exhausted: bool
    """The run's dollar cap was reached: the controller re-raises after recording."""
    usage: dict[str, Any] | None
    """Model usage when the episode paused, so a resume in a fresh process keeps it."""
    continued: bool
    """The controller is continuing a thread that stopped at ``execute`` (the process
    died, or an exception escaped, after the approval): ``execute`` only reconciles."""


Node = Callable[[EpisodeState], dict[str, Any]]


def episode_id_for(mode: RunMode, trigger: Trigger) -> str:
    """The deterministic episode id: the same trigger at the same time gives the same id.

    The run mode is deliberately not part of it. An episode recorded in a backtest
    or a live mode then replays under the id it was recorded with, so the scenario
    ids the roles saw come out the same and the cassette matches. Modes never share
    a state store, so ids cannot collide across them.
    """
    return stable_id("episode", trigger.trigger_id, trigger.at.isoformat())


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _words(text: str, n: int) -> str:
    parts = text.split()
    return " ".join(parts[:n]) + (" …" if len(parts) > n else "")


def trigger_brief(trigger: Trigger, document: Document | None) -> TriggerBrief:
    """What a role sees of the trigger. A price move carries no price: no role sees one."""
    if document is not None:
        return TriggerBrief(
            kind=trigger.kind.value,
            headline=document.title,
            excerpt=_words(document.text, EXCERPT_WORDS),
            source=document.source,
            published_at=document.published_at,
        )
    headline = {
        TriggerKind.PRICE_MOVE: "The event's market prices moved",
        TriggerKind.SCHEDULE: "Scheduled re-check of the event",
    }.get(trigger.kind, "Re-check of the event")
    return TriggerBrief(
        kind=trigger.kind.value,
        headline=headline,
        excerpt="",
        source="pmfa",
        published_at=trigger.at,
    )


def resized(proposal: Proposal, contracts: int, fee_type: str, multiplier: Decimal) -> Proposal:
    """The same proposal with fewer contracts: fee, cost, edge, and target recomputed."""
    from pmfa.decision import fee

    if not 0 < contracts <= proposal.contracts:
        raise ValueError(f"a resize can only shrink the proposal, got {contracts}")
    order_fee = fee(contracts, proposal.limit_price, fee_type, multiplier)
    shrunk = Decimal(str(proposal.shrunk_probability))
    delta = contracts if proposal.side is Side.YES else -contracts
    return Proposal.model_validate(
        {
            **proposal.model_dump(),
            "contracts": contracts,
            "fee": order_fee,
            "cost": contracts * proposal.limit_price + order_fee,
            "edge": shrunk - proposal.limit_price - order_fee / contracts,
            "position_target": proposal.position_before + delta,
        }
    )


def client_order_id(episode_id: str, proposal: Proposal) -> str:
    """Deterministic, so a retry or a second resume cannot double the order."""
    return stable_id(
        "order", episode_id, proposal.proposal_id, proposal.contracts, proposal.limit_price
    )


def order_intent(episode_id: str, proposal: Proposal) -> OrderIntent:
    """The one order a proposal may send, under its deterministic client order id."""
    return OrderIntent(
        client_order_id=client_order_id(episode_id, proposal),
        episode_id=episode_id,
        proposal_id=proposal.proposal_id,
        market_ticker=proposal.market_ticker,
        side=proposal.side,
        contracts=proposal.contracts,
        limit_price=proposal.limit_price,
    )


class Pipeline:
    """The episode's steps over one world. Each public method is one graph node."""

    def __init__(self, world: World, client: StateClient, tracer: Tracer) -> None:
        self.world = world
        self.config = world.config
        self.client = client
        self.tracer = tracer
        self.models = TracingModels(world.models, tracer)

    # ------------------------------------------------------------------ plumbing

    def nodes(self) -> dict[str, Node]:
        """Every node, wrapped so one of the project's errors ends the episode in ``error``."""
        steps: dict[str, Node] = {
            "trigger": self.trigger,
            "match": self.match,
            "research": self.research,
            "forecast_start": self.forecast_start,
            "forecast_step": self.forecast_step,
            "forecast_finish": self.forecast_finish,
            "size": self.size,
            "gate": self.gate,
            "route": self.route,
            "pause": self.pause,
            "review": self.review,
            "execute": self.execute,
            "hold": self.hold,
            "reconcile": self.reconcile,
            "record": self.record,
        }
        return {name: self._guarded(name, fn) for name, fn in steps.items()}

    def _guarded(self, name: str, fn: Node) -> Node:
        """``fn`` with the project's own errors turned into exit ``error``, so ``record`` runs.

        Every episode ends in exactly one named exit, so any ``PmfaError`` raised by a
        node (a model or budget error, an outcome space that cannot be built, an unknown
        fee type, an exchange failure) ends it in ``error`` with nothing sent. Four are
        not caught: a replay that no longer matches its recording (``CassetteMiss``,
        ``RecordedKnowledgeMiss``) stops the replay; a read past the as-of time
        (``AsOfViolation``, the backtest's leakage tripwire among them) must abort the
        run loudly, not become one episode's exit; and an error in ``record`` itself has
        no later node to record it. Other exceptions, LangGraph's review pause among
        them, pass through.
        """
        from pmfa.world.recorded import RecordedKnowledgeMiss

        trace_node = "forecast" if name.startswith("forecast") else name

        def run(state: EpisodeState) -> dict[str, Any]:
            self.models.node = trace_node
            try:
                return fn(state)
            except ModelOutputError as exc:
                return self._exit(state, trace_node, Exit.ERROR, f"model output error: {exc}")
            except RunBudgetExhausted as exc:
                update = self._exit(
                    state, trace_node, Exit.ERROR, f"the run's dollar cap was reached: {exc}"
                )
                return {**update, "run_budget_exhausted": True}
            except BudgetExhausted as exc:
                return self._exit(state, trace_node, Exit.ERROR, f"budget exhausted: {exc}")
            except (CassetteMiss, RecordedKnowledgeMiss, AsOfViolation):
                raise
            except PmfaError as exc:
                if name == "record":
                    raise
                return self._exit(state, trace_node, Exit.ERROR, f"{type(exc).__name__}: {exc}")

        return run

    def _exit(self, state: EpisodeState, node: str, exit: Exit, detail: str) -> dict[str, Any]:
        self.tracer.observation(state["episode_id"], node, f"exit {exit.value}: {detail}")
        return {"exit": exit.value, "exit_detail": detail, "goto": "record"}

    def _as_of(self, state: EpisodeState) -> datetime:
        return _time(state["as_of"])

    def _trigger(self, state: EpisodeState) -> Trigger:
        return Trigger.model_validate(state["trigger"])

    def _stage(self) -> Stage:
        """The stage the gate and routing use.

        The demo exchange runs at the approval stage whatever the stored stage is.
        Its funds are mock, so the stage does not have to be earned there, and a
        human approves every demo order, so it never runs autonomously either.
        Every other mode uses the stored stage, and auto needs the autonomous stage.
        """
        if self.world.mode is RunMode.DEMO:
            return Stage.APPROVAL
        return self.world.store.get_stage().stage

    def _followed(self, event: Event) -> SeriesConfig | None:
        sc = self.config.follow.for_ticker(event.series_ticker)
        return sc if sc is not None and sc.enabled else None

    def _event(self, state: EpisodeState) -> Event:
        ticker = state.get("event_ticker")
        assert ticker, "no event matched yet"
        return self.world.exchange.get_event(ticker)

    def _brief(self, event: Event) -> EventBrief:
        sc = self._followed(event)
        assert sc is not None
        return event_brief(event, build_outcome_space(event, sc), sc)

    def _remember(self, doc_ids: list[str], as_of: datetime) -> None:
        """Remember each cited document's content hash: a later change is a halt condition."""
        from pmfa.world.evidence.common import content_hash

        for doc_id in dict.fromkeys(doc_ids):
            try:
                doc = self.world.evidence.get_document(doc_id, as_of=as_of)
            except NotFound:
                continue
            self.world.store.remember_document(
                doc_id, doc.content_hash or content_hash(doc.text), self.world.clock.now()
            )

    def _sizing_inputs(self, episode_id: str, event: Event, *, fresh: bool) -> SizingInputs:
        """Books, liquidity, holdings, and bankroll as of now. ``fresh`` reads what an order meets."""
        world = self.world
        sc = self._followed(event)
        assert sc is not None
        books: dict[str, OrderBook] = {}
        liquidity: dict[str, dict[Side, Decimal]] = {}
        for market in event.markets:
            if market.status is not MarketStatus.OPEN:
                continue
            try:
                books[market.ticker] = (
                    world.executor.execution_book(market.ticker)
                    if fresh
                    else world.exchange.get_order_book(market.ticker)
                )
                liquidity[market.ticker] = {
                    side: world.exchange.liquidity_dollars(market.ticker, side) for side in Side
                }
            except NotFound:
                continue
        return SizingInputs(
            episode_id=episode_id,
            event_ticker=event.event_ticker,
            forecast_yes={},
            markets=event.markets,
            books=books,
            liquidity=liquidity,
            positions=world.executor.positions(event.event_ticker),
            bankroll=world.executor.balance().equity,
            confidence=0.0,
            series=world.exchange.get_series(event.series_ticker),
            series_config=sc,
            created_at=world.clock.now(),
        )

    def _risk(self, now: datetime) -> RiskState:
        """The risk state at ``now``, on two bases.

        ``bankroll`` is the executor's current equity, the number sizing caps on, so the
        gate's dollar caps agree with the sizer's. The daily loss stop is a fraction of
        the day-start equity, the base the self-halt check and the stage manager's
        loss-stop days use too. An empty equity series has no day start: both bases are
        then the current equity.
        """
        series = self.world.store.equity_series()
        return risk_state(series, now, self.config, bankroll=self.world.executor.balance().equity)

    @staticmethod
    def _mids(sizing: SizingInputs) -> dict[str, str | None]:
        """Each market's YES mid in ``sizing``: its book's, else the market's own quote."""
        mids: dict[str, str | None] = {}
        for market in sizing.markets:
            book = sizing.books.get(market.ticker)
            mid = book.mid_yes() if book is not None else None
            if mid is None:
                mid = market.mid
            mids[market.ticker] = str(mid) if mid is not None else None
        return mids

    def _forecast_mid(self, state: EpisodeState, ticker: str) -> Decimal | None:
        """``ticker``'s YES mid when the forecast started, which no recompute changes."""
        mids = state["forecast_mids"]
        assert mids is not None, "forecast_start sets forecast_mids with the sizing inputs"
        value = mids.get(ticker)
        return Decimal(value) if value is not None else None

    def _engine(self, state: EpisodeState) -> ForecastEngine:
        world, config = self.world, self.config
        eid = state["episode_id"]
        as_of = self._as_of(state)
        sizing = SizingInputs.model_validate(state["sizing"])
        category = sizing.series_config.category
        ticker = sizing.event_ticker
        blocked = set(config.follow.blocklist)
        open_markets = {
            m.ticker
            for m in sizing.markets
            if m.status is MarketStatus.OPEN and m.ticker not in blocked
        }
        series_blocked = sizing.series.ticker in blocked or ticker in blocked

        def more_evidence(query: str) -> EvidenceBatch:
            self.tracer.action(eid, "forecast", f"the critic asked for more evidence: {query}")
            hits = world.knowledge.search_evidence(
                [query], as_of=as_of, category=category, k=config.retrieval.top_k
            )
            if not hits:
                self.tracer.observation(eid, "forecast", "the follow-up found no passages")
                return EvidenceBatch()
            selection = self._select(eid, search_brief, hits, as_of)
            kept = [h.passage for h in hits if h.passage.passage_id in selection]
            origins = world.knowledge.origins(kept, category=category) if kept else []
            self._remember([p.doc_id for p in kept], as_of)
            self.tracer.observation(
                eid,
                "forecast",
                f"the follow-up kept {len(kept)} of {len(hits)} passages",
                passage_ids=[p.passage_id for p in kept],
            )
            return EvidenceBatch(passages=kept, origins=origins)

        search = SearchState.model_validate(state["search"]) if state.get("search") else None
        search_brief = (
            event_brief(search.request.event, search.request.space, search.request.series)
            if search is not None
            else None
        )

        def put_node(node: Any) -> None:
            self.client.put_node(node)

        def on_rejection(rejection: RejectionBrief) -> None:
            self.client.record_rejection(eid, ticker, rejection.claim, rejection.reason)

        return ForecastEngine(
            self.models,
            config,
            size_fn=size_fn(sizing, config),
            more_evidence=more_evidence,
            on_node=put_node,
            on_rejection=on_rejection,
            is_tradable=lambda t: t in open_markets and not series_blocked,
        )

    def _select(
        self,
        eid: str,
        brief: EventBrief | None,
        hits: list[RetrievedPassage],
        as_of: datetime,
    ) -> set[str]:
        """The Researcher's SELECT_PASSAGES: ids it keeps among those offered (others are ignored)."""
        assert brief is not None
        payload = PassageSelectionInput(
            event=brief, passages=[passage_brief(h.passage) for h in hits], as_of=as_of
        )
        result = self.models.call(RoleTask.SELECT_PASSAGES, payload, episode_id=eid)
        selection = result.output
        assert isinstance(selection, PassageSelection)
        offered = {h.passage.passage_id for h in hits}
        return set(selection.keep_passage_ids) & offered

    # ------------------------------------------------------------------ 1. trigger

    def trigger(self, state: EpisodeState) -> dict[str, Any]:
        trigger = self._trigger(state)
        eid = state["episode_id"]
        self.world.store.save_episode(
            EpisodeRecord(
                episode_id=eid,
                mode=RunMode(state["mode"]),
                trigger=trigger,
                as_of=self._as_of(state),
                started_at=_time(state["started_at"]),
            )
        )
        target = f"document {trigger.doc_id}" if trigger.doc_id else f"event {trigger.event_ticker}"
        self.tracer.observation(
            eid,
            "trigger",
            f"{trigger.kind.value} trigger {trigger.trigger_id} at {trigger.at.isoformat()} "
            f"({target})",
            trigger=dump(trigger),
        )
        return {"goto": "match", "excluded": [], "rematched": False, "exit": None}

    # ------------------------------------------------------------------ 2. match

    def match(self, state: EpisodeState) -> dict[str, Any]:
        world, config = self.world, self.config
        eid, trigger, as_of = state["episode_id"], self._trigger(state), self._as_of(state)
        if trigger.kind is not TriggerKind.NEW_DOCUMENT:
            if not trigger.event_ticker:
                return self._exit(state, "match", Exit.NO_MATCH, "the trigger names no event")
            self.tracer.action(
                eid, "match", f"the trigger names event {trigger.event_ticker}: no model call"
            )
            try:
                event = world.exchange.get_event(trigger.event_ticker)
            except NotFound as exc:
                return self._exit(state, "match", Exit.NO_MATCH, str(exc))
            if self._followed(event) is None:
                return self._exit(
                    state, "match", Exit.NO_MATCH, f"series {event.series_ticker} is not followed"
                )
            try:
                self._brief(event)
            except OutcomeSpaceError as exc:
                # The document path skips such a candidate; a named event has no other.
                return self._exit(state, "match", Exit.NO_MATCH, f"outcome space: {exc}")
            self.tracer.observation(eid, "match", f"event {event.event_ticker}: {event.title}")
            return {
                "event_ticker": event.event_ticker,
                "trigger_brief": dump(trigger_brief(trigger, None)),
                "goto": "research",
            }

        try:
            document = world.evidence.get_document(trigger.doc_id or "", as_of=as_of)
        except NotFound as exc:
            return self._exit(state, "match", Exit.NO_MATCH, f"trigger document: {exc}")
        excluded = list(state.get("excluded") or [])
        k = config.retrieval.event_candidates
        self.tracer.action(
            eid,
            "match",
            f"search the events index for the top {k} events as of {as_of.isoformat()}"
            + (f", excluding {', '.join(excluded)}" if excluded else ""),
            k=k,
            excluded=excluded,
        )
        hits = world.knowledge.search_events(
            f"{document.title}\n{document.text}", as_of=as_of, k=k + len(excluded)
        )
        hits = [h for h in hits if h.event_ticker not in excluded][:k]
        briefs: list[EventBrief] = []
        for hit in hits:
            try:
                event = world.exchange.get_event(hit.event_ticker)
            except NotFound:
                continue
            if self._followed(event) is None:
                continue
            try:
                briefs.append(self._brief(event))
            except OutcomeSpaceError:
                continue
        self.tracer.observation(
            eid,
            "match",
            f"{len(briefs)} candidate event(s): {', '.join(b.event_ticker for b in briefs) or 'none'}",
            hits=[dump(h) for h in hits],
        )
        if not briefs:
            return self._exit(state, "match", Exit.NO_MATCH, "no candidate event")
        brief = trigger_brief(trigger, document)
        result = self.models.call(
            RoleTask.MATCH,
            MatchInput(trigger=brief, candidates=briefs, as_of=as_of),
            episode_id=eid,
        )
        decision = result.output
        assert isinstance(decision, MatchDecision)
        candidates = {b.event_ticker for b in briefs}
        if not decision.matched:
            return self._exit(state, "match", Exit.NO_MATCH, "the Monitor matched no candidate")
        if decision.event_ticker not in candidates:
            return self._exit(
                state,
                "match",
                Exit.NO_MATCH,
                f"the Monitor named {decision.event_ticker!r}, which was not a candidate",
            )
        self.tracer.observation(eid, "match", f"matched event {decision.event_ticker}")
        return {
            "event_ticker": decision.event_ticker,
            "trigger_brief": dump(brief),
            "goto": "research",
        }

    # ------------------------------------------------------------------ 3. research

    def research(self, state: EpisodeState) -> dict[str, Any]:
        world, config = self.world, self.config
        eid, trigger, as_of = state["episode_id"], self._trigger(state), self._as_of(state)
        event = self._event(state)
        sc = self._followed(event)
        assert sc is not None
        brief = self._brief(event)
        tbrief = TriggerBrief.model_validate(state["trigger_brief"])
        result = self.models.call(
            RoleTask.PLAN_QUERIES,
            QueryPlanInput(trigger=tbrief, event=brief, as_of=as_of),
            episode_id=eid,
        )
        plan = result.output
        assert isinstance(plan, QueryPlan)
        published_after = None
        if trigger.kind is TriggerKind.PRICE_MOVE:
            published_after = world.store.last_forecast_time(event.event_ticker)
        queries = [plan.query_for, plan.query_against]
        self.tracer.action(
            eid,
            "research",
            f"retrieve the top {config.retrieval.top_k} passages for two queries as of "
            f"{as_of.isoformat()}"
            + (f", published after {published_after.isoformat()}" if published_after else ""),
            queries=queries,
            published_after=published_after.isoformat() if published_after else None,
        )
        hits = world.knowledge.search_evidence(
            queries,
            as_of=as_of,
            category=sc.category,
            k=config.retrieval.top_k,
            published_after=published_after,
        )
        self.tracer.observation(
            eid,
            "research",
            f"{len(hits)} passage(s) retrieved",
            passages=[
                {
                    "passage_id": h.passage.passage_id,
                    "source": h.passage.source,
                    "url": h.passage.url,
                    "published_at": h.passage.published_at.isoformat(),
                    "score": h.score,
                }
                for h in hits
            ],
        )
        if not hits:
            detail = "no passage retrieved"
            if published_after is not None:
                detail = (
                    f"no evidence newer than the last forecast at {published_after.isoformat()}"
                )
            return self._exit(state, "research", Exit.NO_EVIDENCE, detail)

        keep = self._select(eid, brief, hits, as_of)
        kept = [h for h in hits if h.passage.passage_id in keep]
        self.tracer.observation(
            eid,
            "research",
            f"kept {len(kept)} of {len(hits)} passage(s)",
            passage_ids=[h.passage.passage_id for h in kept],
        )
        if not kept:
            return self._exit(
                state, "research", Exit.NO_EVIDENCE, "no retrieved passage bears on the event"
            )

        self.tracer.action(eid, "research", "resolve origins and apply the origin rule")
        origins = world.knowledge.origins([h.passage for h in kept], category=sc.category)
        primary = sum(o.is_primary for o in origins)
        self.tracer.observation(
            eid,
            "research",
            f"{len(origins)} independent origin(s), {primary} primary",
            origins=[dump(o) for o in origins],
        )
        if not origin_rule_met(origins):
            return self._exit(
                state,
                "research",
                Exit.INSUFFICIENT_ORIGINS,
                f"{len(origins)} origin(s), {primary} primary: the rule needs one primary "
                "or two independent secondary origins",
            )
        cited = [h.passage.doc_id for h in kept]
        if trigger.doc_id:
            cited.append(trigger.doc_id)
        self._remember(cited, as_of)
        return {
            "retrieved": [dump(h) for h in kept],
            "origins": [dump(o) for o in origins],
            "goto": "forecast_start",
        }

    # ------------------------------------------------------------------ 4. forecast

    def forecast_start(self, state: EpisodeState) -> dict[str, Any]:
        world, config = self.world, self.config
        eid, as_of = state["episode_id"], self._as_of(state)
        event = self._event(state)
        sc = self._followed(event)
        assert sc is not None
        space = build_outcome_space(event, sc)
        scheduled = world.evidence.scheduled_events(start=as_of, end=event.close_time)
        tree_levels = levels(event, scheduled, as_of, config)
        hits = world.knowledge.search_ledger(
            event.title, as_of=as_of, category=sc.category, k=config.retrieval.ledger_k
        )
        examples = [
            LedgerExample(
                question=h.question,
                forecast_probability=h.probability,
                market_price=h.market_price,
                outcome=h.outcome,
            )
            for h in hits
        ]
        rejections = self.client.get_rejections(event.event_ticker)
        sizing = self._sizing_inputs(eid, event, fresh=False)
        retrieved = [RetrievedPassage.model_validate(r) for r in state.get("retrieved") or []]
        request = ForecastRequest(
            episode_id=eid,
            event=event,
            space=space,
            series=sc,
            levels=tree_levels,
            passages=[r.passage for r in retrieved],
            origins=[Origin.model_validate(o) for o in state.get("origins") or []],
            rejections=rejections,
            ledger_examples=examples,
            as_of=as_of,
            bankroll=sizing.bankroll,
            fast_path=is_fast_path(event, as_of, config),
        )
        update: dict[str, Any] = {"sizing": dump(sizing), "forecast_mids": self._mids(sizing)}
        engine = self._engine({**state, **update})  # type: ignore[typeddict-item]
        search = engine.start(request)
        self.tracer.action(
            eid,
            "forecast",
            f"beam search over {len(search.request.levels)} level(s)"
            + (" on the fast path (the event closes within the hour)" if request.fast_path else "")
            + f"; {len(rejections)} earlier rejection(s), {len(examples)} ledger example(s), "
            f"bankroll ${sizing.bankroll}",
            levels=[dump(lv) for lv in search.request.levels],
        )
        return {**update, "search": dump(search), "goto": "forecast_step"}

    def forecast_step(self, state: EpisodeState) -> dict[str, Any]:
        eid = state["episode_id"]
        search = SearchState.model_validate(state["search"])
        level = search.request.levels[search.level_index]
        depth = search.level_index + 1
        self.tracer.action(
            eid,
            "forecast",
            f"level {depth} of {len(search.request.levels)}: generate, check, and score scenarios "
            f"for {level.title}",
        )
        engine = self._engine(state)
        after = engine.step(search)
        at_depth = [n for n in after.nodes if n.depth == depth]
        counts: dict[str, int] = {}
        for n in at_depth:
            counts[n.status.value] = counts.get(n.status.value, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no scenarios"
        if after.budget_exhausted:
            summary += "; the call budget ran out"
        self.tracer.observation(
            eid,
            "forecast",
            f"level {depth}: {summary}",
            nodes=[
                {
                    "node_id": n.node_id,
                    "parent_id": n.parent_id,
                    "claim": n.claim,
                    "status": n.status.value,
                    "reason": n.reason,
                    "cond_probability": n.cond_probability,
                    "path_mass": n.path_mass,
                    "passage_ids": n.passage_ids,
                }
                for n in at_depth
            ],
        )
        return {
            "search": dump(after),
            "goto": "forecast_finish" if engine.done(after) else "forecast_step",
        }

    def forecast_finish(self, state: EpisodeState) -> dict[str, Any]:
        eid, trigger = state["episode_id"], self._trigger(state)
        search = SearchState.model_validate(state["search"])
        result = self._engine(state).finish(search)
        ticker = search.request.event.event_ticker
        if result.exit is Exit.RULES_MISMATCH:
            can_rematch = trigger.kind is TriggerKind.NEW_DOCUMENT and not state.get("rematched")
            if can_rematch:
                self.tracer.observation(
                    eid,
                    "forecast",
                    f"{result.detail}: match again without {ticker}",
                )
                return {
                    "goto": "match",
                    "rematched": True,
                    "excluded": [*(state.get("excluded") or []), ticker],
                    "event_ticker": None,
                    "retrieved": None,
                    "origins": None,
                    "search": None,
                    "sizing": None,
                    "forecast_mids": None,
                }
            why = (
                "after one re-match"
                if state.get("rematched")
                else "and the trigger named its event, so it cannot re-match"
            )
            return self._exit(state, "forecast", Exit.RULES_MISMATCH, f"{result.detail} {why}")
        if result.exit is not None:
            return self._exit(state, "forecast", result.exit, result.detail)
        forecast = result.forecast
        assert forecast is not None
        self.tracer.observation(
            eid,
            "forecast",
            f"forecast: coverage {forecast.coverage:.3f}, confidence {forecast.confidence:.3f}, "
            f"depth {forecast.depth_reached}, {forecast.n_independent_origins} origin(s)"
            + ("; budget exhausted" if forecast.budget_exhausted else "")
            + (f"; protected {forecast.protected_node_id}" if forecast.protected_node_id else ""),
            forecast=dump(forecast),
            calls={"generator": result.generator_calls, "critic": result.critic_calls},
        )
        return {"forecast": dump(forecast), "goto": "size"}

    # ------------------------------------------------------------------ 5. size

    def size(self, state: EpisodeState) -> dict[str, Any]:
        eid = state["episode_id"]
        forecast = Forecast.model_validate(state["forecast"])
        sizing = SizingInputs.model_validate(state["sizing"])
        if state.get("refresh_books"):
            self.tracer.action(eid, "size", "recompute once with fresh books")
            sizing = self._sizing_inputs(eid, self._event(state), fresh=True)
        inputs = SizingInputs.model_validate(
            {
                **sizing.model_dump(),
                "forecast_yes": dict(forecast.market_probabilities),
                "confidence": forecast.confidence,
                "protected_drove_size": forecast.protected_drove_size,
                "created_at": self.world.clock.now(),
            }
        )
        self.tracer.action(
            eid,
            "size",
            f"size the event: confidence {forecast.confidence:.3f}, bankroll ${inputs.bankroll}",
        )
        decision = size_event(inputs, self.config)
        update: dict[str, Any] = {"sizing": dump(inputs), "refresh_books": False}
        if decision.proposal is None:
            self.tracer.observation(
                eid,
                "size",
                f"no trade: {decision.reason}",
                best=dump(decision.best) if decision.best else None,
                candidates=[dump(c) for c in decision.candidates],
            )
            return {**update, **self._exit(state, "size", Exit.NO_EDGE, decision.reason)}
        p = decision.proposal
        self.tracer.observation(
            eid,
            "size",
            f"proposal: buy {p.contracts} {p.side.value.upper()} {p.market_ticker} at <= "
            f"{p.limit_price}, edge {p.edge:.4f}, cost ${p.cost}",
            proposal=dump(p),
            best=dump(decision.best) if decision.best else None,
        )
        return {**update, "proposal": dump(p), "gate": None, "routing": None, "goto": "gate"}

    # ------------------------------------------------------------------ 6. gate

    def gate(self, state: EpisodeState) -> dict[str, Any]:
        from pmfa.execution.halt import enforce

        world = self.world
        eid = state["episode_id"]
        p = Proposal.model_validate(state["proposal"])
        sizing = SizingInputs.model_validate(state["sizing"])
        forecast = Forecast.model_validate(state["forecast"])
        reviewed = state.get("review") is not None

        # The self-halt conditions run first, so a tripped loss stop or drawdown sets the
        # persisted flag and cancels open orders; the gate re-checks both on its own.
        self.tracer.action(eid, "gate", "check the self-halt conditions")
        stops = enforce(world.store, world.executor, self.config, world.clock, world.evidence)
        if stops:
            world.store.save_proposal(p, ProposalStatus.CANCELLED)
            return self._exit(state, "gate", Exit.HALTED, "self-halt: " + "; ".join(stops))

        book = (
            world.executor.execution_book(p.market_ticker)
            if reviewed
            else sizing.books[p.market_ticker]
        )
        now = world.clock.now()
        origins = [Origin.model_validate(o) for o in state.get("origins") or []]
        search = SearchState.model_validate(state["search"])
        origins += [
            o for o in search.extra_origins if o.origin_id not in {x.origin_id for x in origins}
        ]
        context = GateContext(
            config=self.config,
            now=now,
            halted=world.store.get_halt().halted,
            stage=self._stage(),
            simulated=world.executor.simulated,
            series=sizing.series,
            series_config=sizing.series_config,
            market=world.exchange.get_market(p.market_ticker),
            book=book,
            forecast_yes=forecast.market_probabilities[p.market_ticker],
            origins=origins,
            liquidity_dollars=world.exchange.liquidity_dollars(p.market_ticker, p.side),
            positions=world.executor.positions(p.event_ticker),
            risk=self._risk(now),
        )
        self.tracer.action(
            eid,
            "gate",
            "re-verify every limit"
            + (" at the current price, after the review" if reviewed else ""),
        )
        result = gate(p, context)
        failed = [c for c in result.checks if not c.passed]
        self.tracer.observation(
            eid,
            "gate",
            "approved"
            if result.approved
            else "rejected: " + "; ".join(f"{c.name} ({c.detail})" for c in failed),
            gate=dump(result),
        )
        update: dict[str, Any] = {"gate": dump(result)}
        if not result.approved:
            world.store.save_proposal(p, ProposalStatus.CANCELLED)
            detail = ", ".join(result.reasons)
            if "halt" in result.reasons:
                return {**update, **self._exit(state, "gate", Exit.HALTED, f"gate: {detail}")}
            return {**update, **self._exit(state, "gate", Exit.GATE_REJECTED, detail)}
        return {**update, "goto": "execute" if reviewed else "route"}

    # ------------------------------------------------------------------ 7. route

    def route(self, state: EpisodeState) -> dict[str, Any]:
        from pmfa.execution.halt import halt
        from pmfa.execution.review import ReviewQueue, build_card

        world, config = self.world, self.config
        eid = state["episode_id"]
        p = Proposal.model_validate(state["proposal"])
        sizing = SizingInputs.model_validate(state["sizing"])

        now = world.clock.now()
        # Measured from the forecast-time price, so a recompute cannot re-base the check.
        before = self._forecast_mid(state, p.market_ticker)
        current = world.executor.execution_book(p.market_ticker).mid_yes()
        move = abs(current - before) if before is not None and current is not None else Decimal(1)
        context = RoutingContext(
            config=config,
            stage=self._stage(),
            halted=world.store.get_halt().halted,
            risk=self._risk(now),
            liquidity_dollars=world.exchange.liquidity_dollars(p.market_ticker, p.side),
            calibration=world.store.calibration(sizing.series_config.category, as_of=now),
            price_move=move,
        )
        self.tracer.action(eid, "route", f"score the risk; the YES mid moved {move} since sizing")
        routing = route(p, context)
        note = ""
        if routing.would_be_auto:
            note = "; would be auto, but the stage is below autonomous: sent to review"
        self.tracer.observation(
            eid,
            "route",
            f"tier {routing.tier.value}, score {routing.score}"
            + (f" ({', '.join(routing.reasons)})" if routing.reasons else "")
            + note,
            routing=dump(routing),
        )
        update: dict[str, Any] = {"routing": dump(routing)}
        if routing.tier is RoutingTier.HALT:
            reason = "routing: " + ", ".join(routing.reasons)
            if not world.store.get_halt().halted:
                halt(world.store, world.executor, world.clock, reason)
            return {**update, **self._exit(state, "route", Exit.HALTED, reason)}
        if routing.tier is RoutingTier.AUTO:
            world.store.save_proposal(p, ProposalStatus.APPROVED)
            return {**update, "goto": "execute"}
        if world.review_policy is ReviewPolicy.FILL_TAGGED:
            world.store.save_proposal(p, ProposalStatus.APPROVED)
            self.tracer.observation(
                eid,
                "route",
                "no human present (fill_tagged): the approve-tier proposal is filled and tagged",
            )
            return {**update, "fill_tagged": True, "goto": "execute"}

        event = self._event(state)
        forecast = Forecast.model_validate(state["forecast"])
        retrieved = [RetrievedPassage.model_validate(r) for r in state.get("retrieved") or []]
        card = build_card(p, event, event.market(p.market_ticker), forecast, routing, retrieved)
        ReviewQueue(world.store, world.clock, config).submit(p, card)
        update.update({"card": dump(card), "goto": "pause"})
        merged: EpisodeState = {**state, **update}  # type: ignore[typeddict-item]
        usage = world.models.usage(eid)
        merged["usage"] = dump(usage)
        world.store.save_forecast(forecast)
        self.tracer.observation(
            eid,
            "route",
            f"paused for review until {card.expires_at.isoformat()}",
            card=dump(card),
        )
        world.store.save_episode(self.compose(merged, EpisodeStatus.PENDING_REVIEW))
        return {**update, "usage": dump(usage), "status": EpisodeStatus.PENDING_REVIEW.value}

    # ------------------------------------------------------------------ 8. review

    def pause(self, state: EpisodeState) -> dict[str, Any]:
        """Wait for the human. This node does nothing but interrupt: it re-runs on resume."""
        from langgraph.types import interrupt

        answer = interrupt(state["card"])
        return {"answer": answer, "goto": "review"}

    def review(self, state: EpisodeState) -> dict[str, Any]:
        world = self.world
        eid = state["episode_id"]
        answer = state.get("answer") or {}
        update: dict[str, Any] = {"status": EpisodeStatus.RUNNING.value}
        if answer.get("kind") != "review":
            detail = str(answer.get("detail") or "the proposal expired")
            return {**update, **self._exit(state, "review", Exit.EXPIRED, detail)}
        review = Review.model_validate(answer["review"])
        update["review"] = dump(review)
        code = f" ({review.reason_code.value})" if review.reason_code else ""
        self.tracer.observation(
            eid,
            "review",
            f"{review.reviewer} decided {review.decision.value}"
            + (f" to {review.contracts} contracts" if review.contracts else "")
            + code,
            review=dump(review),
        )
        if review.decision is ReviewDecision.REJECT:
            return {
                **update,
                **self._exit(state, "review", Exit.REJECTED_BY_HUMAN, f"rejected{code}"),
            }
        p = Proposal.model_validate(state["proposal"])
        if review.decision is ReviewDecision.RESIZE:
            assert review.contracts is not None
            sizing = SizingInputs.model_validate(state["sizing"])
            p = resized(p, review.contracts, sizing.series.fee_type, sizing.series.fee_multiplier)
            world.store.save_proposal(p, ProposalStatus.RESIZED)
            self.tracer.action(
                eid, "review", f"resized to {p.contracts} contracts: fee ${p.fee}, cost ${p.cost}"
            )
        return {**update, "proposal": dump(p), "goto": "gate"}

    # ------------------------------------------------------------------ 9. execute

    def execute(self, state: EpisodeState) -> dict[str, Any]:
        """Send the approved proposal as one immediate-or-cancel limit order, at most once.

        The order is looked up by its client order id first (in the store, then through
        ``Executor.find_order``). A thread the controller continued after it stopped here
        (``continued``) only reconciles and never sends: it records the order it finds,
        else ends in ``error`` with nothing placed; when the exchange cannot be asked, the
        outcome is unknown and the episode waits (see below). The approval it ran on may
        be minutes old, its gate ran at an older price, and the human may since have
        rejected or expired the proposal, so sending then would act on a decision nobody
        stands behind. On a first run, the halt flag is read before the book and again
        immediately before sending. When sending raises ``ExchangeError``, the order is
        looked up once: found, it is recorded and the episode ends by what the exchange
        did with it; otherwise the outcome is unknown and the episode waits in ``hold``
        (``_outcome_unknown``). An order the exchange rejected ends in ``error`` with the exchange's
        reason; any order it accepted (filled, partly filled or unfilled) ends
        ``executed``.
        """
        world = self.world
        eid = state["episode_id"]
        p = Proposal.model_validate(state["proposal"])
        intent = order_intent(eid, p)
        if state.get("continued"):
            return self._reconcile(state, p, intent)
        found = self._lookup(eid, intent)
        if found is not None:
            order: OrderResult = found
            world.store.save_order(order)
            self.tracer.observation(eid, "execute", f"order {order.order_id} was already placed")
            return self._order_outcome(state, p, order)

        halted = self._halted(state, p)
        if halted is not None:
            return halted
        if world.clock.now() > p.expires_at:
            # Defensive: the gate that just ran checks the expiry too.
            world.store.save_proposal(p, ProposalStatus.EXPIRED)
            return self._exit(
                state,
                "execute",
                Exit.EXPIRED,
                f"the proposal expired at {p.expires_at.isoformat()} before its order was sent",
            )
        self.tracer.action(eid, "execute", f"re-read the {p.side.value.upper()} offers")
        book = world.executor.execution_book(p.market_ticker)
        offered = [
            lvl for lvl in book.asks(p.side) if lvl.price <= p.limit_price and lvl.contracts > 0
        ]
        best = book.best_ask(p.side)
        if not offered:
            seen = f"best ask {best.price}" if best else "no offer"
            if state.get("review") is None and not state.get("recomputed"):
                world.store.save_proposal(p, ProposalStatus.CANCELLED)
                self.tracer.observation(
                    eid,
                    "execute",
                    f"nothing offered at or below {p.limit_price} ({seen}): no order; recompute once",
                    book=dump(book),
                )
                # The cancelled proposal's gate and routing do not describe what follows.
                return {
                    "recomputed": True,
                    "refresh_books": True,
                    "gate": None,
                    "routing": None,
                    "goto": "size",
                }
            world.store.save_proposal(p, ProposalStatus.CANCELLED)
            return self._exit(
                state,
                "execute",
                Exit.STALE_PRICE,
                f"nothing offered at or below the limit {p.limit_price} ({seen}): no order sent",
            )

        # The book read above can take a while on a live exchange: read the flag again.
        halted = self._halted(state, p)
        if halted is not None:
            return halted
        self.tracer.action(
            eid,
            "execute",
            f"place an immediate-or-cancel limit order: buy {p.contracts} "
            f"{p.side.value.upper()} {p.market_ticker} at <= {p.limit_price}",
            intent=dump(intent),
        )
        try:
            order = world.executor.place(intent)
        except ExchangeError as exc:
            try:
                found = self._find_order(eid, intent)
                looked = f"no order with client order id {intent.client_order_id} was found"
            except PmfaError as failed:
                found = None
                looked = f"the lookup of client order id {intent.client_order_id} failed ({failed})"
            if found is None:
                return self._outcome_unknown(
                    state,
                    "execute",
                    p,
                    f"the exchange failed while the order was sent ({exc}); {looked}",
                )
            order = found
            self.tracer.observation(
                eid,
                "execute",
                f"the exchange failed while the order was sent ({exc}); order "
                f"{order.order_id} was found by its client order id",
            )
        if order.status is not OrderStatus.REJECTED or not order.error.startswith("halted"):
            world.store.save_order(order)
        return self._order_outcome(state, p, order)

    def _halted(self, state: EpisodeState, p: Proposal) -> dict[str, Any] | None:
        """The ``halted`` exit when the persisted flag is set now, else None."""
        halt_state = self.world.store.get_halt()
        if not halt_state.halted:
            return None
        self.world.store.save_proposal(p, ProposalStatus.CANCELLED)
        return self._exit(
            state, "execute", Exit.HALTED, f"the halt flag is set: {halt_state.reason}"
        )

    def _reconcile(self, state: EpisodeState, p: Proposal, intent: OrderIntent) -> dict[str, Any]:
        """Finish a continued thread from what was already sent. Nothing is placed here.

        An order found by its client order id is recorded and ends the episode by what
        the exchange did with it. When the exchange lists none, nothing was placed (the
        order may have been sent and lost): the proposal is cancelled and the episode
        ends ``error``. When the exchange cannot be asked, the episode waits
        ``OUTCOME_UNKNOWN``.
        """
        world = self.world
        eid = state["episode_id"]
        self.tracer.action(
            eid,
            "execute",
            f"continue a thread that stopped here: look up client order id "
            f"{intent.client_order_id} and send nothing",
        )
        try:
            found = self._find_order(eid, intent)
        except PmfaError as exc:
            return self._outcome_unknown(
                state,
                "execute",
                p,
                f"the process stopped at execute and the lookup of client order id "
                f"{intent.client_order_id} failed ({exc}); nothing was sent now",
            )
        if found is not None:
            world.store.save_order(found)
            self.tracer.observation(eid, "execute", f"order {found.order_id} was already placed")
            return self._order_outcome(state, p, found)
        world.store.save_proposal(p, ProposalStatus.CANCELLED)
        return self._exit(
            state,
            "execute",
            Exit.ERROR,
            f"the exchange lists no order with client order id {intent.client_order_id}: "
            "nothing was placed",
        )

    def _outcome_unknown(
        self, state: EpisodeState, node: str, p: Proposal, detail: str
    ) -> dict[str, Any]:
        """Save the proposal and the episode ``OUTCOME_UNKNOWN`` and go to ``hold`` to wait.

        The order may stand on the exchange, so nothing may say it was refused,
        filled or never placed: there is no exit, nothing more is sent for the
        proposal, and the next look is a lookup by the same client order id. The
        ledger entries are written by ``record`` once the outcome is known, as for a
        paused episode.
        """
        world = self.world
        eid = state["episode_id"]
        world.store.save_proposal(p, ProposalStatus.OUTCOME_UNKNOWN)
        if state.get("forecast"):  # kept while it waits, as at the review pause
            world.store.save_forecast(Forecast.model_validate(state["forecast"]))
        self.tracer.observation(
            eid,
            node,
            "outcome unknown, the order may stand on the exchange: nothing more will be sent "
            f"for this proposal, and the order will be looked up again. {detail}",
            detail=detail,
            client_order_id=client_order_id(eid, p),
        )
        update: dict[str, Any] = {
            "status": EpisodeStatus.OUTCOME_UNKNOWN.value,
            "exit": None,
            "exit_detail": "",
            "goto": "hold",
        }
        merged: EpisodeState = {**state, **update}  # type: ignore[typeddict-item]
        world.store.save_episode(self.compose(merged, EpisodeStatus.OUTCOME_UNKNOWN))
        return update

    # ------------------------------------------------------------------ 9b. reconcile

    def hold(self, state: EpisodeState) -> dict[str, Any]:
        """Wait to ask the exchange again. This node does nothing but interrupt: it re-runs
        on resume, so resuming never repeats a side effect."""
        from langgraph.types import interrupt

        p = Proposal.model_validate(state["proposal"])
        interrupt(
            {
                "episode_id": state["episode_id"],
                "client_order_id": client_order_id(state["episode_id"], p),
            }
        )
        return {"goto": "reconcile"}

    def reconcile(self, state: EpisodeState) -> dict[str, Any]:
        """Ask again what became of an order whose outcome is unknown. Nothing is placed here.

        The order is looked up by its client order id, in the store and then through
        ``Executor.find_order``. Found, it is recorded and the episode ends by what the
        exchange did with it: ``executed`` (proposal ``EXECUTED``), or ``error`` for a
        rejected order (proposal ``CANCELLED``). When the exchange answers that it
        lists no such order, nothing was placed: the proposal is cancelled and the
        episode ends ``error``. When the exchange cannot be asked, the episode stays
        ``OUTCOME_UNKNOWN`` and waits in ``hold`` again, as many times as it takes. The
        halt flag does not stop a lookup: it sends nothing.
        """
        world = self.world
        eid = state["episode_id"]
        p = Proposal.model_validate(state["proposal"])
        intent = order_intent(eid, p)
        self.tracer.action(
            eid,
            "reconcile",
            f"look up client order id {intent.client_order_id} again; send nothing",
        )
        try:
            found = self._find_order(eid, intent)
        except PmfaError as exc:
            return self._outcome_unknown(
                state, "reconcile", p, f"the order lookup failed again ({exc})"
            )
        update: dict[str, Any] = {"status": EpisodeStatus.RUNNING.value}
        if found is not None:
            world.store.save_order(found)
            self.tracer.observation(
                eid, "reconcile", f"order {found.order_id} was found by its client order id"
            )
            return {**update, **self._order_outcome(state, p, found, node="reconcile")}
        world.store.save_proposal(p, ProposalStatus.CANCELLED)
        return {
            **update,
            **self._exit(
                state,
                "reconcile",
                Exit.ERROR,
                f"the exchange lists no order with client order id {intent.client_order_id}: "
                "nothing was placed",
            ),
        }

    def _lookup(self, eid: str, intent: OrderIntent) -> OrderResult | None:
        """``_find_order``, with a failed exchange lookup read as no order found."""
        try:
            return self._find_order(eid, intent)
        except ExchangeError:
            return None

    def _find_order(self, eid: str, intent: OrderIntent) -> OrderResult | None:
        """The order already sent for ``intent``, by its client order id, if one exists.

        The store is asked first, then the executor (``Executor.find_order``), since a
        crash or an exchange error can come between the exchange accepting an order and
        the store recording it. Raises ``ExchangeError`` when the exchange cannot be asked.
        """
        for o in self.world.store.list_orders(episode_id=eid):
            if o.client_order_id == intent.client_order_id:
                return o
        try:
            return self.world.executor.find_order(intent)
        except NotFound:
            return None

    def _order_outcome(
        self, state: EpisodeState, p: Proposal, order: OrderResult, *, node: str = "execute"
    ) -> dict[str, Any]:
        """Trace the order and end the episode by what the exchange did with it."""
        world = self.world
        eid = state["episode_id"]
        filled = order.filled_contracts
        self.tracer.observation(
            eid,
            node,
            f"{order.status.value}: {filled} of {order.requested_contracts} filled, "
            f"fees ${order.fees}" + (f", {order.error}" if order.error else ""),
            order=dump(order),
        )
        if order.status is OrderStatus.REJECTED:
            world.store.save_proposal(p, ProposalStatus.CANCELLED)
            if order.error.startswith("halted"):
                return self._exit(state, node, Exit.HALTED, order.error)
            return self._exit(
                state,
                node,
                Exit.ERROR,
                f"the exchange rejected the order: {order.error or 'no reason given'}",
            )
        world.store.save_proposal(p, ProposalStatus.EXECUTED)
        detail = (
            f"{order.status.value}: {filled} of {order.requested_contracts} at <= {p.limit_price}"
        )
        return {"exit": Exit.EXECUTED.value, "exit_detail": detail, "goto": "record"}

    # ------------------------------------------------------------------ 10. record

    def record(self, state: EpisodeState) -> dict[str, Any]:
        world = self.world
        eid = state["episode_id"]
        forecast = Forecast.model_validate(state["forecast"]) if state.get("forecast") else None
        entries = self._ledger_entries(state, forecast)
        if forecast is not None:
            world.store.save_forecast(forecast)
        if entries:
            world.store.add_ledger_entries(entries)
        self.tracer.observation(
            eid,
            "record",
            f"exit {state.get('exit')}: {state.get('exit_detail', '')}; "
            f"{len(entries)} ledger entr{'y' if len(entries) == 1 else 'ies'}",
        )
        world.store.save_episode(self.compose(state, EpisodeStatus.DONE, finished=True))
        self.tracer.forget(eid)
        return {"status": EpisodeStatus.DONE.value, "goto": "end"}

    def _ledger_entries(self, state: EpisodeState, forecast: Forecast | None) -> list[LedgerEntry]:
        if forecast is None or not state.get("sizing"):
            return []
        world = self.world
        eid = state["episode_id"]
        sizing = SizingInputs.model_validate(state["sizing"])
        proposal = Proposal.model_validate(state["proposal"]) if state.get("proposal") else None
        routing = Routing.model_validate(state["routing"]) if state.get("routing") else None
        review = Review.model_validate(state["review"]) if state.get("review") else None
        traded = {
            o.market_ticker for o in world.store.list_orders(episode_id=eid) if o.filled_contracts
        }
        markets = {m.ticker: m for m in sizing.markets}
        entries = []
        for ticker, probability in sorted(forecast.market_probabilities.items()):
            market = markets.get(ticker)
            if market is None:
                continue
            mid = self._forecast_mid(state, ticker)
            ours = proposal is not None and proposal.market_ticker == ticker
            override = ""
            if ours and review is not None and review.decision is not ReviewDecision.APPROVE:
                code = review.reason_code.value if review.reason_code else ""
                override = f"{review.decision.value}:{code}" if code else review.decision.value
            entries.append(
                LedgerEntry(
                    entry_id=stable_id("ledger", eid, ticker),
                    episode_id=eid,
                    mode=RunMode(state["mode"]),
                    event_ticker=forecast.event_ticker,
                    market_ticker=ticker,
                    category=sizing.series_config.category,
                    question=market.title + (f" ({market.subtitle})" if market.subtitle else ""),
                    as_of=forecast.as_of,
                    probability=probability,
                    market_price=float(mid) if mid is not None else None,
                    traded=ticker in traded,
                    tier=routing.tier.value if ours and routing is not None else "",
                    override=override,
                )
            )
        return entries

    def compose(
        self, state: EpisodeState, status: EpisodeStatus, *, finished: bool = False
    ) -> EpisodeRecord:
        """The episode record from the graph state, the store's trace, and the model meter."""
        world = self.world
        eid = state["episode_id"]
        retrieved = [RetrievedPassage.model_validate(r) for r in state.get("retrieved") or []]
        origins = [Origin.model_validate(o) for o in state.get("origins") or []]
        passage_ids = [r.passage.passage_id for r in retrieved]
        if state.get("search"):
            search = SearchState.model_validate(state["search"])
            passage_ids += [p.passage_id for p in search.extra_passages]
            known = {o.origin_id for o in origins}
            origins += [o for o in search.extra_origins if o.origin_id not in known]
        usage = world.models.usage(eid)
        if state.get("usage"):
            before = Usage.model_validate(state["usage"])
            if usage.calls < before.calls:  # a fresh meter in another process
                usage = add_usage(before, usage)
        exit = state.get("exit")
        return EpisodeRecord(
            episode_id=eid,
            mode=RunMode(state["mode"]),
            trigger=self._trigger(state),
            as_of=self._as_of(state),
            event_ticker=state.get("event_ticker"),
            status=status,
            exit=Exit(exit) if exit else None,
            exit_detail=state.get("exit_detail", "") if exit else "",
            passage_ids=list(dict.fromkeys(passage_ids)),
            origins=origins,
            forecast=_load(Forecast, state.get("forecast")),
            proposal=_load(Proposal, state.get("proposal")),
            gate=_load(GateResult, state.get("gate")),
            routing=_load(Routing, state.get("routing")),
            review=_load(Review, state.get("review")),
            orders=world.store.list_orders(episode_id=eid),
            fill_tagged=bool(state.get("fill_tagged")),
            usage=usage,
            trace=world.store.get_trace(eid),
            started_at=_time(state["started_at"]),
            finished_at=world.clock.now() if finished else None,
        )


def _load[R](cls: type[R], value: dict[str, Any] | None) -> R | None:
    return cls.model_validate(value) if value else None  # type: ignore[attr-defined]


__all__ = [
    "EpisodeState",
    "Pipeline",
    "client_order_id",
    "episode_id_for",
    "resized",
    "trigger_brief",
]
