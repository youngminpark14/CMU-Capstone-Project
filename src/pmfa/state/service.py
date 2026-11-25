"""The state service: the operations the MCP state server exposes, as plain Python.

The controller uses these as the roles' shared state, and a reviewer reaches
the same operations through any MCP client. Each method takes typed arguments
and returns records, so the MCP layer (``pmfa.state.server``) is a thin,
one-to-one mapping and the ``StateClient`` can call either side.

Searches go through the ``Knowledge`` port, which enforces the as-of rule
inside the retrieval call. ``as_of`` is therefore a required argument here and
is passed through unchanged: the service never supplies a default.
"""

from __future__ import annotations

from datetime import datetime

from pmfa.errors import NotFound, PmfaError
from pmfa.ports import Exchange, Knowledge, StateStore
from pmfa.records.base import Record
from pmfa.records.episode import (
    Calibration,
    EpisodeRecord,
    EpisodeStatus,
    HaltState,
    LedgerEntry,
    StageState,
    TraceStep,
)
from pmfa.records.evidence import EventHit, LedgerHit, Passage, RetrievedPassage
from pmfa.records.forecast import NodeStatus, TreeNode
from pmfa.records.roles import RejectionBrief


class PortUnavailable(PmfaError):
    """The operation needs a port (Knowledge or Exchange) the service was not given."""


class BeamLevel(Record):
    """The placed scenarios at one depth of an episode's tree, highest path mass first."""

    depth: int
    expanded: list[TreeNode] = []
    """In the beam or the protected slot: their children were generated."""
    frozen: list[TreeNode] = []
    """Outside the beam: not expanded further, still counted in the forecast."""
    leaf: list[TreeNode] = []
    """Scored at the deepest level: counted in the forecast."""


class Beam(Record):
    """An episode's current beam: the expanded, frozen, and leaf scenarios at every depth."""

    episode_id: str
    depth: int
    """The deepest level holding a placed scenario; 0 when there is none yet."""
    levels: list[BeamLevel] = []


class MarketRules(Record):
    ticker: str
    title: str
    subtitle: str = ""
    rules_primary: str = ""
    rules_secondary: str = ""


class EventRules(Record):
    """An event's resolution rules, market by market. Carries no prices."""

    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str = ""
    category: str = ""
    markets: list[MarketRules] = []


_PLACED = {
    NodeStatus.EXPANDED: "expanded",
    NodeStatus.FROZEN: "frozen",
    NodeStatus.LEAF: "leaf",
}


def _require_aware(name: str, moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware time, got {moment!r}")
    return moment


def _by_mass(node: TreeNode) -> tuple[float, str]:
    return (-(node.path_mass if node.path_mass is not None else -1.0), node.node_id)


class StateService:
    """Typed operations over the state store, plus retrieval and rules through the ports."""

    def __init__(
        self,
        store: StateStore,
        knowledge: Knowledge | None = None,
        exchange: Exchange | None = None,
    ) -> None:
        self.store = store
        self._knowledge = knowledge
        self._exchange = exchange

    @property
    def knowledge(self) -> Knowledge:
        if self._knowledge is None:
            raise PortUnavailable(
                "this state service has no Knowledge port: searches and passages are unavailable"
            )
        return self._knowledge

    @property
    def exchange(self) -> Exchange:
        if self._exchange is None:
            raise PortUnavailable("this state service has no Exchange port: rules are unavailable")
        return self._exchange

    # -- scenario tree

    def put_node(self, node: TreeNode) -> TreeNode:
        """Write one scenario-tree node (insert, or update by node id). Returns the stored node."""
        self.store.put_node(node)
        return node

    def get_node(self, episode_id: str, node_id: str) -> TreeNode:
        """Read one scenario-tree node of an episode by its id."""
        for node in self.store.get_nodes(episode_id):
            if node.node_id == node_id:
                return node
        raise NotFound(f"no node {node_id!r} in episode {episode_id!r}")

    def get_nodes(self, episode_id: str, depth: int | None = None) -> list[TreeNode]:
        """Read an episode's scenario-tree nodes, by depth, optionally at one depth only."""
        nodes = self.store.get_nodes(episode_id)
        return [n for n in nodes if depth is None or n.depth == depth]

    def get_beam(self, episode_id: str) -> Beam:
        """The current beam of an episode: expanded, frozen, and leaf scenarios by depth."""
        by_depth: dict[int, dict[str, list[TreeNode]]] = {}
        for node in self.store.get_nodes(episode_id):
            group = _PLACED.get(node.status)
            if group is not None:
                by_depth.setdefault(node.depth, {}).setdefault(group, []).append(node)
        levels = [
            BeamLevel(
                depth=depth,
                **{name: sorted(nodes, key=_by_mass) for name, nodes in groups.items()},
            )
            for depth, groups in sorted(by_depth.items())
        ]
        return Beam(episode_id=episode_id, depth=max(by_depth, default=0), levels=levels)

    def record_rejection(
        self, episode_id: str, event_ticker: str, claim: str, reason: str
    ) -> RejectionBrief:
        """Record a scenario the critic rejected, so the generator does not propose it again."""
        self.store.record_rejection(episode_id, event_ticker, claim, reason)
        return RejectionBrief(claim=claim, reason=reason)

    def get_rejections(self, event_ticker: str) -> list[RejectionBrief]:
        """The critic's rejections recorded for an event, oldest first."""
        return self.store.get_rejections(event_ticker)

    # -- rules and retrieval

    def get_rules(self, event_ticker: str) -> EventRules:
        """An event's resolution rules, market by market, fetched by ticker from the exchange."""
        event = self.exchange.get_event(event_ticker)
        return EventRules(
            event_ticker=event.event_ticker,
            series_ticker=event.series_ticker,
            title=event.title,
            sub_title=event.sub_title,
            category=event.category,
            markets=[
                MarketRules(
                    ticker=m.ticker,
                    title=m.title,
                    subtitle=m.subtitle,
                    rules_primary=m.rules_primary,
                    rules_secondary=m.rules_secondary,
                )
                for m in event.markets
            ],
        )

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        """Evidence passages by id. Ids that do not exist or were published after as_of are left out."""
        knowledge = self.knowledge
        return knowledge.get_passages(passage_ids, as_of=_require_aware("as_of", as_of))

    def search_events(self, query: str, *, as_of: datetime, k: int = 5) -> list[EventHit]:
        """Search the events index: events open at as_of, by similarity to the query."""
        knowledge = self.knowledge
        return knowledge.search_events(query, as_of=_require_aware("as_of", as_of), k=k)

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        """Search the evidence index: the top passages published at or before as_of, ranked."""
        knowledge = self.knowledge
        return knowledge.search_evidence(
            queries,
            as_of=_require_aware("as_of", as_of),
            category=category,
            k=k,
            published_after=published_after,
        )

    def search_ledger(
        self, query: str, *, as_of: datetime, category: str, k: int = 5
    ) -> list[LedgerHit]:
        """Search the ledger index: forecasts resolved before as_of, same category first."""
        knowledge = self.knowledge
        return knowledge.search_ledger(
            query, as_of=_require_aware("as_of", as_of), category=category, k=k
        )

    # -- ledger and calibration

    def query_ledger(
        self,
        resolved_before: datetime | None = None,
        category: str | None = None,
        system: str | None = None,
    ) -> list[LedgerEntry]:
        """Ledger entries, optionally only those resolved before a time, in a category, or of a system."""
        return self.store.ledger(resolved_before=resolved_before, category=category, system=system)

    def calibration(self, category: str, *, as_of: datetime) -> Calibration:
        """Resolved-forecast count and expected calibration error of the pipeline in a category."""
        return self.store.calibration(category, as_of=_require_aware("as_of", as_of))

    # -- episodes

    def list_episodes(
        self,
        status: EpisodeStatus | None = None,
        event_ticker: str | None = None,
        limit: int = 100,
    ) -> list[EpisodeRecord]:
        """Episodes newest first, optionally filtered by status or event, with their traces."""
        return self.store.list_episodes(status=status, event_ticker=event_ticker, limit=limit)

    def get_episode(self, episode_id: str) -> EpisodeRecord:
        """One episode with its full ordered trace."""
        episode = self.store.get_episode(episode_id)
        if episode is None:
            raise NotFound(f"no episode {episode_id!r}")
        return episode

    def get_trace(self, episode_id: str) -> list[TraceStep]:
        """An episode's trace steps (thought, action, observation) in order; empty when none."""
        return self.store.get_trace(episode_id)

    # -- oversight

    def get_halt(self) -> HaltState:
        """Whether the agent is halted, why, and since when."""
        return self.store.get_halt()

    def get_stage(self) -> StageState:
        """The autonomy stage: backtest, shadow, approval, or autonomous."""
        return self.store.get_stage()


READ_OPERATIONS: tuple[str, ...] = (
    "get_node",
    "get_nodes",
    "get_beam",
    "get_rejections",
    "get_rules",
    "get_passages",
    "search_events",
    "search_evidence",
    "search_ledger",
    "query_ledger",
    "calibration",
    "list_episodes",
    "get_episode",
    "get_trace",
    "get_halt",
    "get_stage",
)
"""Service methods that only read. Their MCP tools carry the read-only annotation."""

WRITE_OPERATIONS: tuple[str, ...] = ("put_node", "record_rejection")
"""Service methods that write to the store."""

OPERATIONS: tuple[str, ...] = WRITE_OPERATIONS + READ_OPERATIONS
"""Every service operation; each is one MCP tool of the same name."""
