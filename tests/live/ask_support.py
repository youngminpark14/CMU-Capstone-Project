"""A small Bitcoin world for the ``pmfa ask`` tests, built from one constructed fixture.

``fixtures/constructed_ask.json`` was made by hand from the API shapes (its
``_source`` says so): two open KXBTCD events in Kalshi's wire format (the 1am EST
hour, five strikes, and the 5pm EST event, two strikes), a Coinbase ticker with
the closes of 65 one-minute and 26 hourly candles, and one RSS feed with an item
before the as-of time and one after it. The world puts them behind the ports the
way shadow mode does: a recorded exchange, live evidence over a fake fetcher and
a corpus under ``tmp_path``, an in-memory index, a file-backed store, the paper
executor, and scripted models. Nothing touches the network.

The scripted forecast puts about two thirds on YES for the $59,800 strike, priced
0.47 / 0.49, so a small YES position has an edge after fees and shrinkage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmfa.config import Config
from pmfa.execution.paper import PaperExecutor
from pmfa.knowledge.embedders import HashingEmbedder
from pmfa.knowledge.retriever import ChromaKnowledge
from pmfa.models import ScriptedModels
from pmfa.ports import World
from pmfa.records.base import Record, RunMode
from pmfa.records.decision import ReviewPolicy
from pmfa.records.evidence import Trigger, TriggerKind
from pmfa.records.market import Market, Side
from pmfa.records.roles import (
    GenerateInput,
    GenerateOutput,
    PassageSelection,
    PassageSelectionInput,
    Quantiles,
    QueryPlan,
    RoleTask,
    ScenarioScore,
    ScenarioThought,
    ScoreInput,
    ScoreOutput,
)
from pmfa.state.store import SqliteStore
from pmfa.world.clocks import SimClock
from pmfa.world.evidence.corpus import CorpusStore
from pmfa.world.evidence.http import Fetched, FetchError
from pmfa.world.evidence.live import LiveEvidence
from pmfa.world.kalshi.parse import parse_event, parse_series
from pmfa.world.live import live_corpus_dir, live_paths
from pmfa.world.recorded import RecordedBooks, RecordedExchange, RecordedWorldFile

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "constructed_ask.json").read_text(encoding="utf-8")
)
AS_OF = datetime(2026, 11, 28, 5, 22, tzinfo=UTC)
"""05:22 UTC: the 1am EST event (06:00 UTC) closes in 38 minutes."""
HOUR = "KXBTCD-26NOV2801"
EVENING = "KXBTCD-26NOV2817"
ATM = f"{HOUR}-T59799.99"
"""The strike priced 0.47 / 0.49: the one nearest 0.50."""
COINBASE = "https://api.exchange.coinbase.com"
FEED = "https://cointelegraph.com/rss"


def events() -> list[Any]:
    return [parse_event(raw) for raw in FIXTURE["kalshi_events"]]


def market(ticker: str) -> Market:
    return next(m for e in events() for m in e.markets if m.ticker == ticker)


def world_file(*, settled: Side | None = None) -> RecordedWorldFile:
    """The two events as a recorded file; ``settled`` settles the asked market after its close."""
    listed = events()
    if settled is not None:
        listed = [
            e.model_copy(
                update={
                    "markets": [
                        m.model_copy(
                            update={
                                "result": settled,
                                "settled_at": m.close_time + timedelta(minutes=5),
                            }
                        )
                        if m.ticker == ATM
                        else m
                        for m in e.markets
                    ]
                }
            )
            for e in listed
        ]
    return RecordedWorldFile(
        description="Constructed KXBTCD hour for pmfa ask.",
        provenance="hand-made fixture",
        trigger=Trigger(trigger_id="fixture", kind=TriggerKind.SCHEDULE, at=AS_OF),
        series=[parse_series(FIXTURE["kalshi_series"])],
        events=listed,
        stage="backtest",
    )


# --------------------------------------------------------------------------- fetchers


class FakeFetcher:
    """A Fetcher over a dict of url -> text; a missing url answers 404. Records every call."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, *, refresh: bool = False) -> Fetched:
        self.calls.append(url)
        if url not in self.pages:
            raise FetchError(url, 404, f"{url} answered HTTP 404")
        return Fetched(url=url, status=200, content=self.pages[url].encode(), from_cache=False)


def candle_rows(closes: list[str], seconds: int, end: datetime) -> list[list[Any]]:
    """Coinbase rows ``[time, low, high, open, close, volume]``, newest first, the last one
    starting at ``end`` minus one period; each low and high $5 either side of the close."""
    rows = []
    start = end - timedelta(seconds=seconds * len(closes))
    for i, close in enumerate(closes):
        c = Decimal(close)
        opened = Decimal(closes[i - 1]) if i else c
        t = int((start + timedelta(seconds=seconds * i)).timestamp())
        rows.append([t, float(c - 5), float(c + 5), float(opened), float(c), 1.5])
    return list(reversed(rows))


FUTURE_CLOSE = "99999.00"
"""The close of one extra candle per granularity dated after the clock: it must never be read."""


def coinbase_pages(at: datetime = AS_OF) -> dict[str, str]:
    """The constructed Coinbase answers as of ``at``, each candle list headed by one row that
    starts after ``at`` (closing at ``FUTURE_CLOSE``), which the snapshot must leave out."""
    minute_end = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    hour_end = at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    base = f"{COINBASE}/products/BTC-USD"
    minutes = candle_rows(FIXTURE["coinbase_minute_closes_oldest_first"], 60, minute_end)
    hours = candle_rows(FIXTURE["coinbase_hour_closes_oldest_first"], 3600, hour_end)
    after = candle_rows([FUTURE_CLOSE], 60, minute_end + timedelta(minutes=1))
    hours_after = candle_rows([FUTURE_CLOSE], 3600, hour_end + timedelta(hours=1))
    return {
        f"{base}/ticker": json.dumps(FIXTURE["coinbase_ticker"]),
        f"{base}/candles?granularity=60": json.dumps(after + minutes),
        f"{base}/candles?granularity=3600": json.dumps(hours_after + hours),
    }


# --------------------------------------------------------------------------- scripted roles

ABOVE = "BTC stays above $59,800 into the close"
BELOW = "BTC slips below $59,800 before the close"


def script(*, yes_weight: float = 0.7) -> dict[RoleTask, Any]:
    """Every role's answer: two queries, keep every passage, two scenarios, quantiles each."""

    def select(payload: Record) -> Record:
        assert isinstance(payload, PassageSelectionInput)
        ids = [p.passage_id for p in payload.passages]
        return PassageSelection(keep_passage_ids=ids, dropped=[], rationale="All bear on it.")

    def generate(payload: Record) -> Record:
        assert isinstance(payload, GenerateInput)
        cite = [p.passage_id for p in payload.passages[:1]]
        return GenerateOutput(
            thoughts=[
                ScenarioThought(
                    claim=ABOVE,
                    outcome_ids=[],
                    value_low=59799.99,
                    value_high=None,
                    passage_ids=cite,
                    rationale="The last price is above the strike.",
                ),
                ScenarioThought(
                    claim=BELOW,
                    outcome_ids=[],
                    value_low=None,
                    value_high=59799.99,
                    passage_ids=cite,
                    rationale="A typical hourly move is larger than the gap.",
                ),
            ]
        )

    def score(payload: Record) -> Record:
        assert isinstance(payload, ScoreInput)
        quantiles = {
            ABOVE: Quantiles(q05=59790, q25=59805, q50=59825, q75=59850, q95=59890),
            BELOW: Quantiles(q05=59700, q25=59750, q50=59780, q75=59795, q95=59810),
        }
        weight = {ABOVE: yes_weight, BELOW: 1 - yes_weight}
        return ScoreOutput(
            scores=[
                ScenarioScore(
                    scenario_id=s.scenario_id,
                    verdict="scored",
                    probability=weight[s.claim],
                    option_probabilities=[],
                    quantiles=quantiles[s.claim],
                    rejection_reason="",
                    evidence_request="",
                    rationale="Scored from the price snapshot.",
                )
                for s in payload.scenarios
            ]
        )

    return {
        RoleTask.PLAN_QUERIES: lambda _: QueryPlan(
            query_for="Bitcoin BTC price above $59,800 U.S. dollars",
            query_against="Bitcoin BTC price falls below $59,800",
            rationale="One query each way.",
        ),
        RoleTask.SELECT_PASSAGES: select,
        RoleTask.GENERATE: generate,
        RoleTask.SCORE: score,
    }


# --------------------------------------------------------------------------- the world


def ask_config(data_dir: Path) -> dict[str, Any]:
    """Configuration overrides: files under ``data_dir``, no official sources, no spacing."""
    return {
        "paths": {"data_dir": str(data_dir), "runs_dir": str(data_dir.parent / "runs")},
        "state": {"transport": "direct"},
        "sources": {"federal_reserve": False, "bls": False, "min_seconds_between_requests": 0},
    }


def config_toml(data_dir: Path) -> str:
    return (
        f'[paths]\ndata_dir = "{data_dir}"\nruns_dir = "{data_dir.parent / "runs"}"\n\n'
        '[state]\ntransport = "direct"\n\n'
        "[sources]\nfederal_reserve = false\nbls = false\nmin_seconds_between_requests = 0\n"
    )


@dataclass
class Built:
    """What the fake ``build_world`` made, so a test can look inside."""

    at: datetime = AS_OF
    """The clock of the next world built."""
    worlds: list[World] = field(default_factory=list)
    models: list[ScriptedModels] = field(default_factory=list)
    feeds: list[FakeFetcher] = field(default_factory=list)
    snapshots: list[FakeFetcher] = field(default_factory=list)
    """The fetchers handed to the price snapshot, when a test records them."""


def ask_world(
    config: Config,
    *,
    at: datetime = AS_OF,
    review: bool = True,
    yes_weight: float = 0.7,
    file: RecordedWorldFile | None = None,
    built: Built | None = None,
) -> World:
    """The Bitcoin world in shadow mode, on ``config`` (the asked configuration)."""
    file = file or world_file()
    clock = SimClock(at)
    state_db, _ = live_paths(config, RunMode.SHADOW)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(state_db)
    exchange = RecordedExchange(file, clock, config)
    executor = PaperExecutor(store, clock, config, exchange, RecordedBooks(file))
    if not store.equity_series():
        executor.mark(at - timedelta(hours=1))
    feeds = FakeFetcher({FEED: FIXTURE["feeds"][FEED]})
    models = ScriptedModels(script(yes_weight=yes_weight), config=config)
    if built is not None:
        built.models.append(models)
        built.feeds.append(feeds)
    world = World(
        mode=RunMode.SHADOW,
        config=config,
        clock=clock,
        exchange=exchange,
        executor=executor,
        evidence=LiveEvidence(config, feeds, CorpusStore(live_corpus_dir(config)), clock=clock),
        knowledge=ChromaKnowledge(config, embedder=HashingEmbedder()),
        models=models,
        store=store,
        review_policy=ReviewPolicy.PAUSE if review else ReviewPolicy.FILL_TAGGED,
    )
    if built is not None:
        built.worlds.append(world)
    return world


__all__ = [
    "ABOVE",
    "AS_OF",
    "ATM",
    "BELOW",
    "COINBASE",
    "EVENING",
    "FEED",
    "FIXTURE",
    "FUTURE_CLOSE",
    "HOUR",
    "Built",
    "FakeFetcher",
    "ask_config",
    "ask_world",
    "coinbase_pages",
    "config_toml",
    "events",
    "market",
    "script",
    "world_file",
]
