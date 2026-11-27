"""The forecast engine: beam search over a scenario tree, one level per step.

It turns an event, its evidence and a ``Models`` port into a ``Forecast``. It does
no I/O of its own: model calls go through the ``Models`` port, and sizing, extra
evidence, persistence and tradability come in as callbacks. The episode
controller runs the loop as graph nodes and checkpoints the ``SearchState``
between them::

    state = engine.start(request)
    while not engine.done(state):
        state = engine.step(state)      # generate, check, score and select one level
    result = engine.finish(state)

``run(request)`` does exactly that in one call.

One step, for each parent of the level (the root at the first level, otherwise
every expanded scenario of the level above):

1. GENERATE up to ``max_thoughts_per_node`` thoughts about the level's scheduled event;
2. run the code checks; a failing scenario is deleted with its checks on the node;
3. SCORE the surviving siblings in one call (the critic never sees the
   generator's rationale), honour evidence requests (at most once per scenario
   and ``max_evidence_followups`` per episode) with one re-score, and validate
   the answer in code;

then select: the ``beam_width`` best by ``path mass x evidence support`` (tie rule
in ``support``) are expanded, one more may take the protected slot, the rest are
frozen. At the resolution level every scored scenario is a leaf.

``BudgetExhausted`` stops the search: what was scored so far is aggregated and the
forecast says ``budget_exhausted``. ``ModelOutputError`` propagates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ValidationError

from pmfa.config import Config
from pmfa.errors import BudgetExhausted, ModelOutputError
from pmfa.ports import Models
from pmfa.records.base import Record
from pmfa.records.decision import SizingSummary
from pmfa.records.episode import Exit
from pmfa.records.evidence import Origin, Passage
from pmfa.records.forecast import Forecast, NodeStatus, TreeNode
from pmfa.records.roles import (
    EventBrief,
    GenerateInput,
    GenerateOutput,
    ModelSlot,
    RejectionBrief,
    RoleTask,
    ScheduledEventBrief,
    ScoreInput,
    ScoreOutput,
)

from .aggregate import aggregate, subtree_ids
from .checks import run_checks
from .outcomes import event_brief
from .support import beam_order, evidence_support
from .tree import (
    apply_scores,
    check_one_score_each,
    make_node,
    passage_brief,
    path_steps,
    scenario_brief,
)
from .types import (
    EvidenceBatch,
    ForecastRequest,
    ForecastResult,
    IsTradable,
    MoreEvidence,
    OnNode,
    OnRejection,
    SearchState,
    SizeFn,
)


def _as[R: Record](cls: type[R], output: object) -> R:
    if isinstance(output, cls):
        return output
    try:
        return cls.model_validate(output)
    except ValidationError as exc:
        raise ModelOutputError(f"expected a {cls.__name__}: {exc}") from exc


def _traded_cost(size: SizingSummary) -> Decimal:
    return Decimal(size.cost) if size.trades else Decimal("0")


def size_move(a: SizingSummary, b: SizingSummary) -> Decimal:
    """How far apart two sizing results are, in dollars of cost.

    The cost difference when both buy the same side of the same market (or either
    does not trade); the sum of both costs when they trade different things.
    """
    if a.trades and b.trades and (a.market_ticker, a.side) != (b.market_ticker, b.side):
        return _traded_cost(a) + _traded_cost(b)
    return abs(_traded_cost(a) - _traded_cost(b))


@dataclass
class _Work:
    """A mutable working copy of a ``SearchState`` during one step."""

    nodes: dict[str, TreeNode]
    extra_passages: list[Passage]
    extra_origins: list[Origin]
    rejections: list[RejectionBrief]
    evidence_followups: int
    generator_calls: int
    critic_calls: int
    protected_node_ids: list[str]
    budget_exhausted: bool = False
    exit: Exit | None = None
    exit_detail: str = ""
    emitted: dict[str, TreeNode] = field(default_factory=dict)
    """Nodes already passed to ``on_node`` during this step."""

    @classmethod
    def of(cls, state: SearchState) -> _Work:
        return cls(
            nodes={n.node_id: n for n in state.nodes},
            extra_passages=list(state.extra_passages),
            extra_origins=list(state.extra_origins),
            rejections=list(state.rejections),
            evidence_followups=state.evidence_followups,
            generator_calls=state.generator_calls,
            critic_calls=state.critic_calls,
            protected_node_ids=list(state.protected_node_ids),
            budget_exhausted=state.budget_exhausted,
            exit=state.exit,
            exit_detail=state.exit_detail,
        )

    def put(self, node: TreeNode) -> None:
        self.nodes[node.node_id] = node


class ForecastEngine:
    """Beam search over scenarios for one event. Dependencies are fixed; the state is not."""

    def __init__(
        self,
        models: Models,
        config: Config,
        *,
        size_fn: SizeFn,
        more_evidence: MoreEvidence | None = None,
        on_node: OnNode | None = None,
        on_rejection: OnRejection | None = None,
        is_tradable: IsTradable | None = None,
    ) -> None:
        self.models = models
        self.config = config
        self.size_fn = size_fn
        self.more_evidence = more_evidence
        self.on_node = on_node
        self.on_rejection = on_rejection
        self.is_tradable: IsTradable = is_tradable or (lambda _ticker: True)

    # ------------------------------------------------------------------ public steps

    def run(self, request: ForecastRequest) -> ForecastResult:
        state = self.start(request)
        while not self.done(state):
            state = self.step(state)
        return self.finish(state)

    def start(self, request: ForecastRequest) -> SearchState:
        levels = list(request.levels)
        if not levels or not levels[-1].is_resolution:
            raise ValueError("the last level must be the event's resolution")
        max_depth = self.config.forecast.max_depth
        if request.fast_path:
            levels = [levels[-1]]
        elif len(levels) > max_depth:
            levels = [*levels[: max_depth - 1], levels[-1]]
        return SearchState(request=request, levels=levels, rejections=list(request.rejections))

    def done(self, state: SearchState) -> bool:
        if state.exit is not None or state.budget_exhausted:
            return True
        if state.level_index >= len(state.levels):
            return True
        if state.level_index == 0:
            return False
        return not any(
            n.depth == state.level_index and n.status is NodeStatus.EXPANDED for n in state.nodes
        )

    def step(self, state: SearchState) -> SearchState:
        """Generate, check, score and select one level."""
        if self.done(state):
            return state
        work = _Work.of(state)
        level = state.levels[state.level_index]
        depth = state.level_index + 1
        brief = event_brief(state.request.event, state.request.space, state.request.series)
        if depth == 1:
            parents: list[TreeNode | None] = [None]
        else:
            parents = [
                n for n in state.nodes if n.depth == depth - 1 and n.status is NodeStatus.EXPANDED
            ]
        try:
            for parent in parents:
                self._expand(state, work, brief, parent, level, depth)
        except BudgetExhausted:
            work.budget_exhausted = True
        finally:
            self._emit(state, work)

        if depth == 1:
            first = [n for n in work.nodes.values() if n.depth == 1]
            if first and all(_failed(n, "rules") for n in first):
                work.exit = Exit.RULES_MISMATCH
                work.exit_detail = "every first-level scenario failed the rules check"
        if work.exit is None and not work.budget_exhausted:
            self._select(state, work, depth, last=state.level_index == len(state.levels) - 1)
            self._emit(state, work)
        return state.model_copy(
            update={
                "nodes": list(work.nodes.values()),
                "level_index": state.level_index + 1,
                "extra_passages": work.extra_passages,
                "extra_origins": work.extra_origins,
                "rejections": work.rejections,
                "evidence_followups": work.evidence_followups,
                "generator_calls": work.generator_calls,
                "critic_calls": work.critic_calls,
                "protected_node_ids": work.protected_node_ids,
                "budget_exhausted": work.budget_exhausted,
                "exit": work.exit,
                "exit_detail": work.exit_detail,
            }
        )

    def finish(self, state: SearchState) -> ForecastResult:
        """The forecast from what was scored, or the ``rules_mismatch`` / ``low_coverage`` exit."""
        common = {
            "nodes": state.nodes,
            "budget_exhausted": state.budget_exhausted,
            "generator_calls": state.generator_calls,
            "critic_calls": state.critic_calls,
        }
        if state.exit is not None:
            return ForecastResult(exit=state.exit, detail=state.exit_detail, **common)
        forecast = self._aggregate(state, state.nodes)
        floor = self.config.forecast.coverage_floor
        if forecast is None or forecast.coverage < floor:
            coverage = forecast.coverage if forecast is not None else 0.0
            return ForecastResult(
                exit=Exit.LOW_COVERAGE,
                detail=f"coverage {coverage:.3f} is below the floor {floor}",
                **common,
            )
        if state.protected_node_ids:
            final = self.size_fn(forecast)
            best: tuple[str, Decimal] | None = None
            for pid in state.protected_node_ids:
                alt = self._aggregate(state, state.nodes, exclude=subtree_ids(state.nodes, pid))
                move = size_move(final, self.size_fn(alt) if alt else SizingSummary())
                if best is None or move > best[1]:
                    best = (pid, move)
            assert best is not None
            forecast = forecast.model_copy(
                update={
                    "protected_node_id": best[0],
                    "protected_drove_size": best[1] > self._risk_unit(state),
                }
            )
        return ForecastResult(forecast=forecast, **common)

    # ------------------------------------------------------------------ one parent

    def _expand(
        self,
        state: SearchState,
        work: _Work,
        brief: EventBrief,
        parent: TreeNode | None,
        level: ScheduledEventBrief,
        depth: int,
    ) -> None:
        request = state.request
        fc = self.config.forecast
        passages = self._passages(request, work)
        level_titles = {lv.scheduled_event_id: lv.title for lv in state.levels}
        payload = GenerateInput(
            event=brief,
            level=level,
            path=path_steps(parent, work.nodes, level_titles),
            passages=[
                passage_brief(p) for p in passages.values() if p.published_at <= request.as_of
            ],
            rejected=list(work.rejections),
            max_thoughts=fc.max_thoughts_per_node,
            as_of=request.as_of,
        )
        slot = ModelSlot.FAST_PATH if request.fast_path else ModelSlot.GENERATOR
        result = self.models.call(
            RoleTask.GENERATE, payload, episode_id=request.episode_id, slot=slot
        )
        work.generator_calls += 1
        output = _as(GenerateOutput, result.output)

        counted = self._counted_origins(request, work)
        survivors: list[TreeNode] = []
        for index, thought in enumerate(output.thoughts[: fc.max_thoughts_per_node]):
            node = make_node(
                episode_id=request.episode_id,
                event_ticker=request.event.event_ticker,
                parent=parent,
                level=level,
                depth=depth,
                index=index,
                thought=thought,
            )
            checks = run_checks(
                node,
                level,
                request.space,
                passages,
                counted,
                request.as_of,
                request.event.close_time,
            )
            failed = [c.name for c in checks if not c.passed]
            if failed:
                node = node.model_copy(
                    update={
                        "checks": checks,
                        "status": NodeStatus.DELETED,
                        "reason": "failed check: " + ", ".join(failed),
                    }
                )
            else:
                node = node.model_copy(update={"checks": checks})
                survivors.append(node)
            work.put(node)
        if survivors:
            self._score(state, work, brief, parent, level, depth, survivors)

    def _score(
        self,
        state: SearchState,
        work: _Work,
        brief: EventBrief,
        parent: TreeNode | None,
        level: ScheduledEventBrief,
        depth: int,
        siblings: list[TreeNode],
    ) -> None:
        request = state.request
        fc = self.config.forecast
        if request.fast_path:
            slot = ModelSlot.FAST_PATH
        else:
            slot = ModelSlot.CRITIC_DEPTH1 if depth == 1 else ModelSlot.CRITIC_DEEP
        cited = list(dict.fromkeys(pid for n in siblings for pid in n.passage_ids))
        first = self._critic(state, work, brief, parent, level, siblings, cited, slot)
        check_one_score_each(siblings, first)

        added: list[str] = []
        if self.more_evidence is not None:
            for score in first.scores:
                node = work.nodes[score.scenario_id]
                query = score.evidence_request.strip()
                if (
                    score.verdict != "scored"
                    or not query
                    or node.evidence_requested
                    or work.evidence_followups >= fc.max_evidence_followups
                ):
                    continue
                work.evidence_followups += 1
                work.put(node.model_copy(update={"evidence_requested": True}))
                added += self._add_evidence(request, work, self.more_evidence(query))

        siblings = [work.nodes[n.node_id] for n in siblings]
        final, exhausted = first, False
        if added:
            try:
                final = self._critic(
                    state, work, brief, parent, level, siblings, cited + added, slot
                )
            except BudgetExhausted:
                exhausted = True

        parent_mass = 1.0 if parent is None else (parent.path_mass or 0.0)
        scored, rejections = apply_scores(siblings, final, request.space, parent_mass)
        passages = self._passages(request, work)
        origins = {o.origin_id: o for o in [*request.origins, *work.extra_origins]}
        for node in scored:
            if node.status is NodeStatus.SCORED:
                support = evidence_support(
                    node.passage_ids,
                    passages,
                    origins,
                    request.as_of,
                    request.series.recency_half_life_days,
                )
                node = node.model_copy(
                    update={
                        "evidence_support": support.value,
                        "n_origins": support.n_origins,
                        "freshest_evidence_at": support.freshest,
                    }
                )
            work.put(node)
        for rejection in rejections:
            work.rejections.append(rejection)
            if self.on_rejection is not None:
                self.on_rejection(rejection)
        if exhausted:
            raise BudgetExhausted("the re-score after an evidence follow-up hit the budget")

    def _critic(
        self,
        state: SearchState,
        work: _Work,
        brief: EventBrief,
        parent: TreeNode | None,
        level: ScheduledEventBrief,
        siblings: list[TreeNode],
        passage_ids: list[str],
        slot: ModelSlot,
    ) -> ScoreOutput:
        request = state.request
        passages = self._passages(request, work)
        level_titles = {lv.scheduled_event_id: lv.title for lv in state.levels}
        payload = ScoreInput(
            event=brief,
            level=level,
            path=path_steps(parent, work.nodes, level_titles),
            scenarios=[scenario_brief(n) for n in siblings],
            passages=[
                passage_brief(passages[pid])
                for pid in passage_ids
                if pid in passages and passages[pid].published_at <= request.as_of
            ],
            ledger_examples=list(request.ledger_examples),
            as_of=request.as_of,
        )
        result = self.models.call(RoleTask.SCORE, payload, episode_id=request.episode_id, slot=slot)
        work.critic_calls += 1
        return _as(ScoreOutput, result.output)

    # ------------------------------------------------------------------ selection

    def _select(self, state: SearchState, work: _Work, depth: int, *, last: bool) -> None:
        candidates = [
            n for n in work.nodes.values() if n.depth == depth and n.status is NodeStatus.SCORED
        ]
        if last:
            for n in candidates:
                work.put(n.model_copy(update={"status": NodeStatus.LEAF, "reason": "leaf"}))
            return
        order = beam_order(candidates, self.config.forecast.tie_band)
        width = self.config.forecast.beam_width
        beam, rest = order[:width], order[width:]
        for n in beam:
            work.put(n.model_copy(update={"status": NodeStatus.EXPANDED, "reason": "beam"}))
        protected = self._protected(state, work, rest)
        for n in rest:
            if protected is not None and n.node_id == protected:
                work.put(
                    n.model_copy(update={"status": NodeStatus.EXPANDED, "reason": "protected"})
                )
                work.protected_node_ids.append(n.node_id)
            else:
                work.put(
                    n.model_copy(update={"status": NodeStatus.FROZEN, "reason": "outside the beam"})
                )

    def _protected(self, state: SearchState, work: _Work, rest: list[TreeNode]) -> str | None:
        """The scenario outside the beam whose removal would change the decision most."""
        if not rest:
            return None
        nodes = list(work.nodes.values())
        base = self._aggregate(state, nodes, work=work)
        if base is None:
            return None
        base_size = self.size_fn(base)
        risk_unit = self._risk_unit(state)
        best: tuple[str, Decimal] | None = None
        for n in rest:
            alt = self._aggregate(state, nodes, work=work, exclude=frozenset({n.node_id}))
            alt_size = self.size_fn(alt) if alt is not None else SizingSummary()
            move = size_move(base_size, alt_size)
            flips = base_size.trades and alt_size.trades and base_size.side != alt_size.side
            extreme = alt is not None and any(
                p in (0.0, 1.0)
                and base.market_probabilities.get(ticker) != p
                and self.is_tradable(ticker)
                for ticker, p in alt.market_probabilities.items()
            )
            if (flips or move > risk_unit or extreme) and (best is None or move > best[1]):
                best = (n.node_id, move)
        return best[0] if best is not None else None

    # ------------------------------------------------------------------ helpers

    def _risk_unit(self, state: SearchState) -> Decimal:
        return Decimal(state.request.bankroll) * self.config.caps.risk_unit_fraction

    @staticmethod
    def _passages(request: ForecastRequest, work: _Work) -> dict[str, Passage]:
        return {p.passage_id: p for p in [*request.passages, *work.extra_passages]}

    @staticmethod
    def _counted_origins(request: ForecastRequest, work: _Work) -> set[str]:
        return {o.origin_id for o in [*request.origins, *work.extra_origins]}

    def _add_evidence(self, request: ForecastRequest, work: _Work, batch: object) -> list[str]:
        """Add a follow-up's passages (none published after the as-of time); return their ids."""
        batch = _as(EvidenceBatch, batch)
        known = self._passages(request, work)
        added: list[str] = []
        for p in batch.passages:
            if p.passage_id in known or p.published_at > request.as_of:
                continue
            work.extra_passages.append(p)
            known[p.passage_id] = p
            added.append(p.passage_id)
        counted = self._counted_origins(request, work)
        for o in batch.origins:
            if o.origin_id not in counted and o.published_at <= request.as_of:
                work.extra_origins.append(o)
                counted.add(o.origin_id)
        return added

    def _aggregate(
        self,
        state: SearchState,
        nodes: list[TreeNode],
        *,
        work: _Work | None = None,
        exclude: frozenset[str] = frozenset(),
    ) -> Forecast | None:
        request = state.request
        extra_passages = work.extra_passages if work is not None else state.extra_passages
        extra_origins = work.extra_origins if work is not None else state.extra_origins
        exhausted = work.budget_exhausted if work is not None else state.budget_exhausted
        return aggregate(
            nodes,
            request.space,
            episode_id=request.episode_id,
            as_of=request.as_of,
            passages={p.passage_id: p for p in [*request.passages, *extra_passages]},
            counted_origins={o.origin_id for o in [*request.origins, *extra_origins]},
            full_origins=self.config.forecast.confidence_full_origins,
            budget_exhausted=exhausted,
            fast_path=request.fast_path,
            exclude=exclude,
        )

    def _emit(self, state: SearchState, work: _Work) -> None:
        if self.on_node is None:
            return
        before = {n.node_id: n for n in state.nodes}
        for node_id, node in work.nodes.items():
            if work.emitted.get(node_id, before.get(node_id)) != node:
                self.on_node(node)
                work.emitted[node_id] = node


def _failed(node: TreeNode, check: str) -> bool:
    return any(c.name == check and not c.passed for c in node.checks)
