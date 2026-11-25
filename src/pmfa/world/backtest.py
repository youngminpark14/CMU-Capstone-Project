"""The backtest world: the frozen snapshot and corpus behind the episode's ports, read as of a clock.

``BacktestData`` opens the two halves of the frozen data (``pmfa data build``
writes them under ``config.paths``) and builds, or reopens, a persistent
retrieval index over them. The index directory is named by a key made of the
snapshot manifest, the corpus manifest, the embedder's identity, the chunking
settings, the canary text and ``INDEX_FORMAT``, so a rebuilt snapshot or
corpus, another embedder, or code that stores rows of another shape gets a
fresh index and never a stale one.

The index also holds a **canary**: a document dated a day after the end of
the window whose text contains ``config.evaluation.canary_text`` and the words
every query uses. It is placed in the evidence index only, not in the corpus,
so the Evidence port never sees it, and the as-of filter inside retrieval is
the only thing that keeps it out. ``CanaryGuard`` wraps the Knowledge port
and raises ``LeakageDetected`` the moment any retrieval returns one of its
passages; the runner also scans every finished episode for it. ``LeakageDetected``
is an ``AsOfViolation``, which the episode pipeline never turns into an exit,
so a leak aborts the run wherever it is found.

``build_backtest_world`` assembles the ports for one simulated clock and one
state store. There is no web access anywhere in it: the exchange is the
snapshot, the evidence is the stored corpus, and nothing holds a fetcher.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pmfa.config import Config
from pmfa.errors import AsOfViolation, ConfigError
from pmfa.ports import Clock, Models, StateStore, World
from pmfa.records.base import RunMode
from pmfa.records.decision import ReviewPolicy
from pmfa.records.episode import EpisodeRecord
from pmfa.records.evidence import Document, Passage, RetrievedPassage
from pmfa.world.evidence.calendar import ReleaseCalendar
from pmfa.world.evidence.common import content_hash
from pmfa.world.evidence.corpus import CorpusManifest, CorpusStore, FrozenEvidence
from pmfa.world.snapshot.book import ExecutionBook, execution_book
from pmfa.world.snapshot.reader import SnapshotExchange
from pmfa.world.snapshot.store import SnapshotManifest, SnapshotStore, snapshot_path

if TYPE_CHECKING:
    from pmfa.knowledge.retriever import ChromaKnowledge

INDEX_MARKER = "pmfa_backtest_index.json"
"""Written into a finished index directory; an index without it is rebuilt."""

INDEX_FORMAT = 2
"""The shape of the rows the index stores, part of the index key.

Bump it whenever the stored rows change shape, so an index cached by older code
is rebuilt rather than reused. 2: the events index holds one row per listing
change of an event instead of one row with its full strike list.
"""

CANARY_SOURCE = "canary.invalid"
CANARY_TOPIC = (
    "federal funds rate decision hold cut hike basis points FOMC statement Committee "
    "consumer price index CPI inflation twelve months percent increase release"
)
"""Words every baseline and pipeline query shares, so the canary would rank first if it leaked."""


class LeakageDetected(AsOfViolation):
    """A retrieval or an episode reached the canary document: the as-of rule failed. The run aborts.

    It is an ``AsOfViolation`` so that the episode pipeline re-raises it instead of
    ending one episode in exit ``error`` and letting the run continue.
    """


def backtest_config(config: Config) -> Config:
    """``config`` as a backtest runs it: the roles' shared state goes to the state service directly.

    The in-process MCP transport exercises the same service methods at a
    throughput cost that a backtest of many episodes does not need, so
    ``state.transport`` is forced to ``direct``. Everything else is unchanged.
    """
    return config.model_copy(
        update={"state": config.state.model_copy(update={"transport": "direct"})}
    )


# --------------------------------------------------------------------------- the canary guard


def canary_hits(passages: Iterable[Passage], *, canary_doc_id: str, canary_text: str) -> list[str]:
    """Passage ids among ``passages`` that come from the canary or carry its text."""
    marker = canary_text.split()[0] if canary_text.split() else canary_text
    return [
        p.passage_id
        for p in passages
        if p.doc_id == canary_doc_id
        or p.passage_id.startswith(canary_doc_id + "#")
        or (marker and marker in p.text)
    ]


class CanaryGuard:
    """The Knowledge port with a tripwire: any passage of the canary raises ``LeakageDetected``.

    Every method of the wrapped knowledge is forwarded; ``search_evidence`` and
    ``get_passages`` are checked before their results are returned.
    """

    def __init__(
        self,
        inner: Any,
        *,
        canary_doc_id: str,
        canary_text: str,
        _counts: dict[str, int] | None = None,
    ) -> None:
        self.inner = inner
        self.canary_doc_id = canary_doc_id
        self.canary_text = canary_text
        self._counts = _counts if _counts is not None else {"retrievals": 0}

    @property
    def checked_retrievals(self) -> int:
        """Retrieval calls checked so far, by this guard and every ``with_ranking`` variant of it."""
        return self._counts["retrievals"]

    def _check(self, passages: list[Passage], where: str) -> None:
        self._counts["retrievals"] += 1
        hits = canary_hits(passages, canary_doc_id=self.canary_doc_id, canary_text=self.canary_text)
        if hits:
            raise LeakageDetected(
                f"{where} returned the canary document ({', '.join(hits)}), which is dated after "
                "the end of the window: the as-of rule failed and the run is aborted"
            )

    def search_evidence(
        self,
        queries: list[str],
        *,
        as_of: datetime,
        category: str,
        k: int = 8,
        published_after: datetime | None = None,
    ) -> list[RetrievedPassage]:
        hits = self.inner.search_evidence(
            queries, as_of=as_of, category=category, k=k, published_after=published_after
        )
        self._check([h.passage for h in hits], f"search_evidence as of {as_of.isoformat()}")
        return hits

    def get_passages(self, passage_ids: list[str], *, as_of: datetime) -> list[Passage]:
        found = self.inner.get_passages(passage_ids, as_of=as_of)
        self._check(found, f"get_passages as of {as_of.isoformat()}")
        return found

    def with_ranking(self, ranking: str) -> CanaryGuard:
        """The same index and guard with ``retrieval.ranking`` set to ``ranking``.

        The ranking variants of the single-pass baseline share one index; only
        the ranking step differs, so the inner knowledge is copied shallowly
        with a changed configuration. The variant shares this guard's count of
        checked retrievals.
        """
        variant = copy.copy(self.inner)
        config: Config = variant.config
        variant.config = config.model_copy(
            update={"retrieval": config.retrieval.model_copy(update={"ranking": ranking})}
        )
        return CanaryGuard(
            variant,
            canary_doc_id=self.canary_doc_id,
            canary_text=self.canary_text,
            _counts=self._counts,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def assert_episode_clean(episode: EpisodeRecord, *, canary_doc_id: str, canary_text: str) -> None:
    """Raise ``LeakageDetected`` when a finished episode cites or saw the canary anywhere."""
    text = episode.model_dump_json()
    marker = canary_text.split()[0] if canary_text.split() else canary_text
    if canary_doc_id in text or (marker and marker in text):
        raise LeakageDetected(
            f"episode {episode.episode_id} (as of {episode.as_of.isoformat()}) contains the "
            "canary document: the as-of rule failed and the run is aborted"
        )


# --------------------------------------------------------------------------- the data


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class BacktestData:
    """The frozen snapshot and corpus, the release calendar, the canary, and the retrieval index.

    ``index_root`` defaults to ``config.paths.index_dir()``. The index is built
    on first use (``ensure_index``) and reused while its key matches.
    """

    def __init__(self, config: Config, *, index_root: str | Path | None = None) -> None:
        self.config = config
        path = snapshot_path(config)
        if not path.exists():
            raise ConfigError(f"no snapshot at {path}: run `pmfa data build` first")
        self.snapshot = SnapshotStore(path)
        snapshot_manifest = self.snapshot.manifest()
        if snapshot_manifest is None:
            raise ConfigError(f"the snapshot at {path} has no manifest: rebuild it")
        self.snapshot_manifest: SnapshotManifest = snapshot_manifest
        self.corpus = CorpusStore(config.paths.corpus_dir())
        corpus_manifest = self.corpus.manifest()
        if corpus_manifest is None:
            raise ConfigError(
                f"no corpus manifest in {self.corpus.directory}: run `pmfa data build` first"
            )
        self.corpus_manifest: CorpusManifest = corpus_manifest
        self.calendar = ReleaseCalendar(self.corpus.events())
        self.window_end: datetime = self.snapshot_manifest.window_end
        if self.corpus_manifest.window_end is not None:
            self.window_end = max(self.window_end, self.corpus_manifest.window_end)

        from pmfa.knowledge.embedders import make_embedder

        self.embedder_identity = make_embedder(config.retrieval.embedder).identity
        retrieval = config.retrieval
        material = {
            # What was dropped does not change what is indexed, and leaving it out
            # keeps the key of a snapshot built before the field existed.
            "snapshot": self.snapshot_manifest.model_dump(mode="json", exclude={"dropped_events"}),
            "corpus": self.corpus_manifest.model_dump(mode="json"),
            "embedder": self.embedder_identity,
            "chunking": [
                retrieval.passage_words,
                retrieval.passage_overlap_words,
                retrieval.near_duplicate_similarity,
                retrieval.origin_copy_overlap,
            ],
            "canary": config.evaluation.canary_text,
            "series": [s.ticker for s in config.follow.enabled_series()],
            "format": INDEX_FORMAT,
        }
        self.index_key = hashlib.sha256(_stable_json(material).encode()).hexdigest()[:16]
        root = Path(index_root) if index_root is not None else config.paths.index_dir()
        self.index_path = root / f"backtest-{self.index_key}"
        self.canary = self._canary()

    # -- reading

    def events(self) -> list[Any]:
        """Every stored event of an enabled series, with its final markets, earliest close first."""
        enabled = {s.ticker for s in self.config.follow.enabled_series()}
        out = []
        for series in self.snapshot.series_tickers():
            if series not in enabled:
                continue
            for ticker in self.snapshot.event_tickers(series):
                event = self.snapshot.get_event(ticker)
                if event.markets:
                    out.append(event)
        return sorted(out, key=lambda e: (e.close_time, e.event_ticker))

    def _canary(self) -> Document:
        text = (
            f"{self.config.evaluation.canary_text}. {CANARY_TOPIC}. "
            "This text is placed in the evidence index to prove that retrieval never returns "
            "a document published after the as-of time."
        )
        doc_id = f"canary-{self.index_key}"
        return Document(
            doc_id=doc_id,
            source=CANARY_SOURCE,
            kind="canary",
            url=f"https://{CANARY_SOURCE}/{doc_id}",
            title="Canary: federal funds rate decision and consumer price index",
            published_at=self.window_end + timedelta(days=1),
            text=text,
            content_hash=content_hash(text),
        )

    # -- the index

    def ensure_index(self) -> Path:
        """Build the persistent index for this key unless a finished one exists. Returns its path."""
        if (self.index_path / INDEX_MARKER).exists():
            return self.index_path
        from pmfa.knowledge.retriever import ChromaKnowledge

        building = self.index_path.with_name(self.index_path.name + ".building")
        shutil.rmtree(building, ignore_errors=True)
        knowledge = ChromaKnowledge(self.config, path=building)
        try:
            documents = [*self.corpus.documents(), self.canary]
            passages = knowledge.index_documents(documents)
            events = knowledge.index_events(self.events())
        finally:
            knowledge.close()
        marker = {
            "key": self.index_key,
            "embedder": self.embedder_identity,
            "documents": len(documents),
            "passages": passages,
            "events": events,
            "canary_doc_id": self.canary.doc_id,
            "canary_published_at": self.canary.published_at.isoformat(),
        }
        (building / INDEX_MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(self.index_path, ignore_errors=True)
        os.replace(building, self.index_path)
        return self.index_path

    def index_info(self) -> dict[str, Any]:
        """What the index holds, from its marker file (building it first if needed)."""
        path = self.ensure_index()
        info = json.loads((path / INDEX_MARKER).read_text(encoding="utf-8"))
        return {**info, "path": str(path)}

    def knowledge(self, working_copy: str | Path | None = None) -> ChromaKnowledge:
        """The retrieval index: a fresh working copy in ``working_copy``, or the shared one.

        A run writes resolved forecasts into the ledger index, so each pass of a
        run gets its own copy and the shared index keeps only documents and
        events.
        """
        from pmfa.knowledge.retriever import ChromaKnowledge

        base = self.ensure_index()
        if working_copy is None:
            return ChromaKnowledge(self.config, path=base)
        target = Path(working_copy)
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(base, target)
        return ChromaKnowledge(self.config, path=target)

    def guard(self, knowledge: Any) -> CanaryGuard:
        return CanaryGuard(
            knowledge,
            canary_doc_id=self.canary.doc_id,
            canary_text=self.config.evaluation.canary_text,
        )

    def execution_book(self, ticker: str, as_of: datetime) -> ExecutionBook:
        """The book an order sent at ``as_of`` meets, with how fine the candle behind it was."""
        return execution_book(self.snapshot, ticker, as_of, self.config)

    def close(self) -> None:
        self.snapshot.close()


# --------------------------------------------------------------------------- the world


def build_backtest_world(
    data: BacktestData,
    config: Config,
    *,
    clock: Clock,
    store: StateStore,
    models: Models,
    knowledge: Any | None = None,
) -> World:
    """The ports of a backtest, all read as of ``clock``.

    - Exchange: ``SnapshotExchange`` over the snapshot.
    - Evidence: ``FrozenEvidence`` over the corpus and its release calendar.
    - Knowledge: ``knowledge`` (default: the shared index) behind a ``CanaryGuard``.
    - Executor: a ``PaperExecutor`` with two books. ``execution_book``, which the
      pipeline decides with (the price re-read before sending, the one
      recompute, the second gate, routing's price move) and which marks equity,
      is the exchange's as-of book: nothing priced after the clock's time. The
      order itself fills against the snapshot's execution book at the clock's
      time (the first one-minute candle after the as-of time plus the latency,
      on the conservative side, else the hourly fallback, else empty), which no
      decision sees. A limit that candle does not meet leaves the order unfilled.
    - Review policy ``FILL_TAGGED``: with no human present, approve-tier
      proposals are filled and tagged, so reports can separate them.
    - ``state.transport`` is forced to ``direct`` for throughput (see ``backtest_config``).
    """
    from pmfa.execution.paper import PaperExecutor

    config = backtest_config(config)
    exchange = SnapshotExchange(data.snapshot, clock, config)
    evidence = FrozenEvidence(data.corpus, data.calendar)
    guarded = (
        knowledge
        if isinstance(knowledge, CanaryGuard)
        else data.guard(knowledge if knowledge is not None else data.knowledge())
    )

    def fill_books(ticker: str) -> Any:
        return execution_book(data.snapshot, ticker, clock.now(), config).book

    executor = PaperExecutor(
        store, clock, config, exchange, exchange.get_order_book, fill_books=fill_books
    )
    return World(
        mode=RunMode.BACKTEST,
        config=config,
        clock=clock,
        exchange=exchange,
        executor=executor,
        evidence=evidence,
        knowledge=guarded,  # type: ignore[arg-type]
        models=models,
        store=store,
        review_policy=ReviewPolicy.FILL_TAGGED,
    )


__all__ = [
    "CANARY_SOURCE",
    "INDEX_FORMAT",
    "BacktestData",
    "CanaryGuard",
    "LeakageDetected",
    "assert_episode_clean",
    "backtest_config",
    "build_backtest_world",
    "canary_hits",
]
