"""Small recorded worlds and scripted roles for the episode tests.

A Fed-decision event (five markets) and a CPI ladder (five strikes), their books,
a handful of documents, the release calendar, and a ``Script`` that answers
every role from tables: the Monitor names an event, the Researcher plans two
queries and keeps every passage, the generator proposes scenarios keyed by
(level, parent claim), and the critic scores them by claim.

Default Fed prices (YES bid / ask) and the hawkish forecast the critic gives::

    hike >25  0.01 / 0.03     0.01
    hike 25   0.02 / 0.04     0.06
    hold      0.61 / 0.63     0.70
    cut 25    0.32 / 0.34     0.18   <- NO at 0.68 has the best edge after shrinkage
    cut >25   0.01 / 0.03     0.05
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmfa.episode import EpisodeController
from pmfa.models import ScriptedModels
from pmfa.records.base import Record, RunMode
from pmfa.records.decision import ReviewPolicy
from pmfa.records.episode import EpisodeRecord, LedgerEntry
from pmfa.records.evidence import Document, ScheduledEvent, Trigger, TriggerKind
from pmfa.records.market import BookLevel, Event, Market, MarketStatus, OrderBook, Series
from pmfa.records.roles import (
    GenerateInput,
    GenerateOutput,
    MatchDecision,
    OptionProbability,
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
from pmfa.world.evidence.common import content_hash
from pmfa.world.recorded import (
    RecordedBooks,
    RecordedWorld,
    RecordedWorldFile,
    build_recorded_world,
    recorded_config,
)

AS_OF = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
FED_CLOSE = datetime(2026, 12, 9, 18, 59, tzinfo=UTC)
FED = "KXFEDDECISION-26DEC"
FED_JAN = "KXFEDDECISION-27JAN"
H26, H25, H0, C25, C26 = (f"{FED}-{s}" for s in ("H26", "H25", "H0", "C25", "C26"))
CPI = "KXCPIYOY-26NOV"
CANARY = "CANARY-7f3a91 this document is dated after every forecast and must never be retrieved"

FED_OPTIONS = {
    "H26": ("Hike", ">25", "Hike >25bps"),
    "H25": ("Hike", "25", "Hike 25bps"),
    "H0": ("Hike", "0", "Fed maintains rate"),
    "C25": ("Cut", "25", "Cut 25bps"),
    "C26": ("Cut", ">25", "Cut >25bps"),
}
DEFAULT_PRICES = {
    "H26": ("0.01", "0.03"),
    "H25": ("0.02", "0.04"),
    "H0": ("0.61", "0.63"),
    "C25": ("0.32", "0.34"),
    "C26": ("0.01", "0.03"),
}
PHANTOM_PRICES = {  # the market already prices the hawkish minutes
    "H26": ("0.01", "0.02"),
    "H25": ("0.05", "0.07"),
    "H0": ("0.69", "0.71"),
    "C25": ("0.17", "0.19"),
    "C26": ("0.04", "0.06"),
}
HAWKISH = {"hike_gt25": 0.01, "hike_25": 0.06, "hold": 0.70, "cut_25": 0.18, "cut_gt25": 0.05}
DEPTH = "1000"

# --------------------------------------------------------------------------- exchange


def fed_series() -> Series:
    return Series(
        ticker="KXFEDDECISION",
        title="Fed decision",
        category="Economics",
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal("1"),
    )


def cpi_series() -> Series:
    return Series(
        ticker="KXCPIYOY",
        title="CPI year over year",
        category="Economics",
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal("1"),
    )


def fed_event(
    ticker: str = FED,
    prices: dict[str, tuple[str, str]] | None = None,
    close: datetime = FED_CLOSE,
) -> Event:
    prices = prices or DEFAULT_PRICES
    markets = []
    for suffix, (direction, amount, label) in FED_OPTIONS.items():
        bid, ask = prices[suffix]
        markets.append(
            Market(
                ticker=f"{ticker}-{suffix}",
                event_ticker=ticker,
                title=f"Fed decision on {close:%b %d, %Y}: {label}",
                subtitle=label,
                status=MarketStatus.OPEN,
                open_time=datetime(2026, 9, 1, tzinfo=UTC),
                close_time=close,
                strike_type="custom",
                custom_strike={direction: amount},
                rules_primary=(
                    f"If the Federal Reserve does a {direction} of {amount}bps on "
                    f"{close:%B %d, %Y}, then the market resolves to Yes."
                ),
                rules_secondary="The outcomes are mutually exclusive.",
                yes_bid=bid,
                yes_ask=ask,
            )
        )
    return Event(
        event_ticker=ticker,
        series_ticker="KXFEDDECISION",
        title=f"Fed decision in {close:%b %Y}?",
        category="Economics",
        mutually_exclusive=True,
        markets=markets,
    )


CPI_STRIKES = ("3.4", "3.5", "3.6", "3.7", "3.8")
CPI_PRICES = {"3.4": "0.85", "3.5": "0.65", "3.6": "0.45", "3.7": "0.25", "3.8": "0.10"}


def cpi_event() -> Event:
    close = datetime(2026, 12, 10, 13, 29, tzinfo=UTC)
    markets = []
    for strike in CPI_STRIKES:
        mid = Decimal(CPI_PRICES[strike])
        markets.append(
            Market(
                ticker=f"{CPI}-T{strike}",
                event_ticker=CPI,
                title=f"CPI year over year in November 2026: above {strike}%",
                subtitle=f"Above {strike}%",
                status=MarketStatus.OPEN,
                open_time=datetime(2026, 10, 1, tzinfo=UTC),
                close_time=close,
                strike_type="greater",
                floor_strike=Decimal(strike),
                rules_primary=(
                    f"If the Consumer Price Index increases by more than {strike}% in the twelve "
                    "months ending November 2026, then the market resolves to Yes."
                ),
                yes_bid=mid - Decimal("0.01"),
                yes_ask=mid + Decimal("0.01"),
            )
        )
    return Event(
        event_ticker=CPI,
        series_ticker="KXCPIYOY",
        title="CPI YoY in November 2026",
        category="Economics",
        markets=markets,
    )


def book(
    ticker: str,
    yes_bids: list[tuple[str, str]],
    no_bids: list[tuple[str, str]],
    at: datetime = AS_OF,
) -> OrderBook:
    """A book from (price, contracts) levels, best first. A NO bid at q offers YES at 1 - q."""
    return OrderBook(
        ticker=ticker,
        as_of=at,
        yes_bids=[BookLevel(price=p, contracts=c) for p, c in yes_bids],
        no_bids=[BookLevel(price=p, contracts=c) for p, c in no_bids],
    )


def books_for(event: Event, depth: str = DEPTH) -> dict[str, OrderBook]:
    """One deep level per side at each market's quote."""
    out = {}
    for m in event.markets:
        assert m.yes_bid is not None and m.yes_ask is not None
        out[m.ticker] = book(
            m.ticker, [(str(m.yes_bid), depth)], [(str(Decimal(1) - m.yes_ask), depth)]
        )
    return out


def no_book(ticker: str, yes_bids: list[tuple[str, str]], yes_ask: str = "0.34") -> OrderBook:
    """A C25-style book where buying NO meets ``yes_bids`` (NO asks at 1 - bid)."""
    return book(ticker, yes_bids, [(str(Decimal(1) - Decimal(yes_ask)), DEPTH)])


# --------------------------------------------------------------------------- evidence


def document(
    doc_id: str,
    title: str,
    text: str,
    published_at: datetime,
    *,
    source: str = "federalreserve.gov",
    kind: str = "fomc_minutes",
) -> Document:
    return Document(
        doc_id=doc_id,
        source=source,
        kind=kind,
        url=f"https://{source}/{doc_id}.htm",
        title=title,
        published_at=published_at,
        text=text,
        content_hash=content_hash(text),
    )


MINUTES = document(
    "fomc-minutes-2026-10",
    "Minutes of the Federal Open Market Committee, October 2026",
    "Participants judged that inflation remained elevated and that the federal funds rate "
    "should stay restrictive. Many participants saw the case for holding the federal funds "
    "rate steady at the December meeting; few saw a rate cut in December as appropriate.",
    AS_OF - timedelta(minutes=30),
)
SPEECH = document(
    "speech-2026-11-12",
    "Governor remarks on the economic outlook",
    "I would support holding the federal funds rate steady in December while inflation "
    "data settle, and I see little case for a rate cut at the December meeting.",
    AS_OF - timedelta(days=8),
    kind="speech",
)
FUTURE_CANARY = document(
    "canary-2026-11-21",
    "Federal funds rate decision December",
    f"{CANARY}. The Committee held the federal funds rate steady in December; no rate cut.",
    AS_OF + timedelta(days=1),
)
NEWS = document(
    "news-fed-2026-11-20",
    "Fed officials lean toward holding rates in December",
    "Officials signalled the federal funds rate would likely stay steady in December "
    "with no rate cut, according to people familiar with the discussions.",
    AS_OF - timedelta(hours=2),
    source="news.example",
    kind="news",
)


def syndicated(doc: Document, n: int, source: str) -> Document:
    return doc.model_copy(
        update={
            "doc_id": f"{doc.doc_id}-copy{n}",
            "source": source,
            "url": f"https://{source}/{doc.doc_id}-copy{n}",
            "published_at": doc.published_at + timedelta(minutes=10 * n),
        }
    )


CPI_RELEASE = document(
    "bls-ppi-2026-11",
    "Producer Price Index, October 2026",
    "Producer prices rose 0.4 percent in October; consumer price index inflation for "
    "November is expected near 3.6 percent over twelve months.",
    AS_OF - timedelta(hours=1),
    source="bls.gov",
    kind="cpi_release",
)
JOBS_DEC = ScheduledEvent(
    scheduled_event_id="jobs:2026-11",
    kind="employment_situation",
    title="Employment Situation for November 2026",
    scheduled_at=datetime(2026, 12, 4, 13, 30, tzinfo=UTC),
    period="2026-11",
)
FOMC_DEC = ScheduledEvent(
    scheduled_event_id="fomc:2026-12",
    kind="fomc_decision",
    title="FOMC decision, December 2026",
    scheduled_at=datetime(2026, 12, 9, 19, 0, tzinfo=UTC),
)


def doc_trigger(doc: Document = MINUTES, at: datetime = AS_OF) -> Trigger:
    return Trigger(
        trigger_id=f"trig-{doc.doc_id}", kind=TriggerKind.NEW_DOCUMENT, at=at, doc_id=doc.doc_id
    )


def calibrated_ledger(n: int = 30) -> list[LedgerEntry]:
    """``n`` resolved, well-calibrated Fed forecasts (calibration error 0.05)."""
    entries = []
    for i in range(n):
        high = i % 2 == 0
        resolved = AS_OF - timedelta(days=40 + i)
        entries.append(
            LedgerEntry(
                entry_id=f"past-{i}",
                episode_id=f"past-ep-{i}",
                mode=RunMode.BACKTEST,
                event_ticker=f"KXFEDDECISION-PAST{i}",
                market_ticker=f"KXFEDDECISION-PAST{i}-H0",
                category="fed_decision",
                question=f"Past Fed decision {i}: hold?",
                as_of=resolved - timedelta(days=3),
                probability=0.95 if high else 0.05,
                market_price=0.9 if high else 0.1,
                outcome=high,
                resolved_at=resolved,
            )
        )
    return entries


def fed_file(**changes: Any) -> RecordedWorldFile:
    """The hawkish-minutes world: the default Fed event, its books, the minutes and a speech."""
    event = changes.pop("event", None) or fed_event()
    base: dict[str, Any] = {
        "description": "Hawkish October minutes before the December Fed meeting.",
        "provenance": "hand-made fixture",
        "trigger": doc_trigger(),
        "series": [fed_series()],
        "events": [event],
        "order_books": books_for(event),
        "documents": [MINUTES, SPEECH, FUTURE_CANARY],
        "scheduled_events": [FOMC_DEC],
        "stage": "approval",
    }
    base.update(changes)
    return RecordedWorldFile.model_validate(base)


def cpi_file(**changes: Any) -> RecordedWorldFile:
    event = cpi_event()
    base: dict[str, Any] = {
        "description": "A producer-price release before the November CPI.",
        "provenance": "hand-made fixture",
        "trigger": doc_trigger(CPI_RELEASE),
        "series": [cpi_series()],
        "events": [event],
        "order_books": books_for(event),
        "documents": [CPI_RELEASE],
        "stage": "approval",
    }
    base.update(changes)
    return RecordedWorldFile.model_validate(base)


# --------------------------------------------------------------------------- scripted roles


@dataclass(frozen=True)
class T:
    """A scenario thought. ``cite`` defaults to the first passage the generator is shown."""

    claim: str
    outcomes: tuple[str, ...] = ()
    low: float | None = None
    high: float | None = None
    cite: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Spec:
    """How the critic scores one claim."""

    probability: float
    options: dict[str, float] | None = None
    quantiles: tuple[float, float, float, float, float] | None = None
    verdict: str = "scored"
    reason: str = ""
    evidence_request: str = ""


RESOLUTION = "R"
"""The key of the resolution level in a ``Script.tree``."""

DEFAULT_PLAN = QueryPlan(
    query_for="federal funds rate steady December hold inflation",
    query_against="rate cut December meeting",
    rationale="One query for a hold, one for a cut.",
)


def keep_all(payload: PassageSelectionInput) -> list[str]:
    return [p.passage_id for p in payload.passages]


@dataclass
class Script:
    """Every role's answers. ``fail`` makes a task raise instead of answering."""

    matches: list[str | None] = field(default_factory=lambda: [FED])
    plan: QueryPlan = DEFAULT_PLAN
    keep: Callable[[PassageSelectionInput], list[str]] = keep_all
    tree: dict[tuple[str, str | None], list[T]] = field(default_factory=dict)
    scores: dict[str, Spec] = field(default_factory=dict)
    fail: dict[RoleTask, BaseException] = field(default_factory=dict)

    def models(self, file: RecordedWorldFile) -> ScriptedModels:
        matches: Iterator[str | None] = iter(self.matches)
        last: list[str | None] = [None]

        def match(_: Record) -> Record:
            last[0] = next(matches, last[0])
            ticker = last[0]
            return MatchDecision(
                matched=ticker is not None,
                event_ticker=ticker or "",
                rationale=f"The trigger bears on {ticker}." if ticker else "Unrelated.",
            )

        def select(payload: Record) -> Record:
            assert isinstance(payload, PassageSelectionInput)
            keep = self.keep(payload)
            offered = {p.passage_id for p in payload.passages}
            return PassageSelection(
                keep_passage_ids=keep,
                dropped=[],
                rationale=f"Kept {len(set(keep) & offered)} of {len(offered)}.",
            )

        def generate(payload: Record) -> Record:
            assert isinstance(payload, GenerateInput)
            key = RESOLUTION if payload.level.is_resolution else payload.level.scheduled_event_id
            parent = payload.path[-1].claim if payload.path else None
            first = [p.passage_id for p in payload.passages[:1]]
            return GenerateOutput(
                thoughts=[
                    ScenarioThought(
                        claim=t.claim,
                        outcome_ids=list(t.outcomes),
                        value_low=t.low,
                        value_high=t.high,
                        passage_ids=list(t.cite) if t.cite is not None else first,
                        rationale=f"Generator rationale for {t.claim}.",
                    )
                    for t in self.tree.get((key, parent), [])
                ]
            )

        def score(payload: Record) -> Record:
            assert isinstance(payload, ScoreInput)
            out = []
            for s in payload.scenarios:
                spec = self.scores[s.claim]
                out.append(
                    ScenarioScore(
                        scenario_id=s.scenario_id,
                        verdict=spec.verdict,  # type: ignore[arg-type]
                        probability=spec.probability,
                        option_probabilities=[
                            OptionProbability(outcome_id=k, probability=v)
                            for k, v in (spec.options or {}).items()
                        ],
                        quantiles=Quantiles(
                            q05=spec.quantiles[0],
                            q25=spec.quantiles[1],
                            q50=spec.quantiles[2],
                            q75=spec.quantiles[3],
                            q95=spec.quantiles[4],
                        )
                        if spec.quantiles
                        else None,
                        rejection_reason=spec.reason,
                        evidence_request=spec.evidence_request,
                        rationale=f"Critic on {s.claim}.",
                    )
                )
            return ScoreOutput(scores=out)

        answers: dict[RoleTask, Callable[[Record], Record]] = {
            RoleTask.MATCH: match,
            RoleTask.PLAN_QUERIES: lambda _: self.plan,
            RoleTask.SELECT_PASSAGES: select,
            RoleTask.GENERATE: generate,
            RoleTask.SCORE: score,
        }
        for task, exc in self.fail.items():
            answers[task] = _raiser(exc)
        return ScriptedModels(answers, config=recorded_config(file))


def _raiser(exc: BaseException) -> Callable[[Record], Record]:
    def raise_it(_: Record) -> Record:
        raise exc

    return raise_it


def hawkish_script(dist: dict[str, float] | None = None, **changes: Any) -> Script:
    """One level (the resolution): a hold scenario and a cut scenario, both with ``dist``."""
    dist = dist or HAWKISH
    script = Script(
        tree={
            (RESOLUTION, None): [
                T("The Committee holds the rate in December", ("hold",)),
                T("The Committee cuts 25 bp in December", ("cut_25",)),
            ]
        },
        scores={
            "The Committee holds the rate in December": Spec(0.7, dist),
            "The Committee cuts 25 bp in December": Spec(0.3, dist),
        },
    )
    return dataclasses.replace(script, **changes)


# --------------------------------------------------------------------------- running


def make_world(
    file: RecordedWorldFile,
    script: Script,
    *,
    policy: ReviewPolicy = ReviewPolicy.FILL_TAGGED,
    mode: RunMode = RunMode.REPLAY,
) -> RecordedWorld:
    return build_recorded_world(file, models=script.models(file), review_policy=policy, mode=mode)


def with_transport(file: RecordedWorldFile, transport: str) -> RecordedWorldFile:
    overrides = dict(file.config_overrides)
    overrides["state"] = {"transport": transport}
    return file.model_copy(update={"config_overrides": overrides})


def run_episode(
    file: RecordedWorldFile,
    script: Script,
    *,
    policy: ReviewPolicy = ReviewPolicy.FILL_TAGGED,
    mode: RunMode = RunMode.REPLAY,
    transport: str = "direct",
) -> tuple[EpisodeRecord, RecordedWorld]:
    """Build the world, run the file's trigger to an exit or the pause, and check the trace."""
    world = make_world(with_transport(file, transport), script, policy=policy, mode=mode)
    with EpisodeController(world) as controller:
        record = controller.run(file.trigger)
    check_trace(record)
    return record, world


def file_world(
    file: RecordedWorldFile,
    directory: Path,
    script: Script,
    *,
    policy: ReviewPolicy = ReviewPolicy.PAUSE,
    seed: bool,
) -> RecordedWorld:
    """A recorded world whose store is a file, as the live modes have it.

    ``seed`` writes the stage and the first equity point (the first process
    does); a second world built over the same file leaves it as it is.
    """
    from pmfa.execution.paper import PaperExecutor
    from pmfa.state.store import SqliteStore

    world = make_world(file, script, policy=policy)
    store = SqliteStore(directory / "state.sqlite")
    if seed:
        store.set_stage(file.stage, "recorded world", file.as_of)
    executor = PaperExecutor(
        store,
        world.clock,
        world.config,
        world.exchange,
        RecordedBooks(file),
        bankroll=file.bankroll,
    )
    if seed:
        executor.mark(file.as_of)
    return dataclasses.replace(world, store=store, executor=executor)


def check_trace(record: EpisodeRecord) -> None:
    """The trace starts at the trigger, ends at record, and is numbered 1..n in order."""
    trace = record.trace
    assert trace, "an episode always has a trace"
    assert trace[0].node == "trigger"
    if record.exit is not None:
        assert trace[-1].node == "record"
        assert record.exit.value in trace[-1].summary
    assert [s.seq for s in trace] == list(range(1, len(trace) + 1))


def advance(world: RecordedWorld, seconds: float) -> None:
    world.clock.advance(seconds)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- an exchange in doubt


def exchange_account(world: RecordedWorld) -> Any:
    """A second paper account standing in for the exchange's own record of orders."""
    from pmfa.execution.paper import PaperExecutor
    from pmfa.state.store import MEMORY, SqliteStore

    return PaperExecutor(
        SqliteStore(MEMORY), world.clock, world.config, world.exchange, RecordedBooks(world.file)
    )


class DoubtfulExchange:
    """An executor whose answers about an order are scripted, for unknown order outcomes.

    ``place`` records the intent in ``placed``, hands it to ``exchange`` (the exchange's
    own record, which fills it) when ``accept`` is set, and then raises ``ExchangeError``,
    as an answer lost on the way back would. Before any order is sent the exchange
    answers lookups; after one is sent, ``find_order`` raises ``ExchangeError`` for the
    next ``failures`` lookups (``crash`` instead raises ``RuntimeError`` once, as a
    process dying there would), and then asks ``exchange``. Everything else is the
    inner executor's. ``placed`` may be shared, so several worlds count one exchange.
    """

    def __init__(
        self,
        inner: Any,
        exchange: Any,
        *,
        accept: bool = True,
        failures: int = 1,
        placed: list[Any] | None = None,
    ) -> None:
        self._inner = inner
        self.exchange = exchange
        self.accept = accept
        self.failures = failures
        self.crash = False
        self.placed: list[Any] = placed if placed is not None else []

    def place(self, intent: Any) -> Any:
        from pmfa.errors import ExchangeError

        self.placed.append(intent)
        if self.accept:
            self.exchange.place(intent)
        raise ExchangeError("POST /portfolio/events/orders -> 504 gateway_timeout")

    def find_order(self, intent: Any) -> Any:
        from pmfa.errors import ExchangeError

        if self.placed and self.crash:
            self.crash = False
            raise RuntimeError("the process died during the order lookup")
        if self.placed and self.failures > 0:
            self.failures -= 1
            raise ExchangeError("GET /portfolio/orders -> 503 service_unavailable")
        return self.exchange.find_order(intent)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def doubtful(world: RecordedWorld, exchange: Any = None, **settings: Any) -> RecordedWorld:
    """``world`` with its executor wrapped in a ``DoubtfulExchange`` (see there)."""
    account = exchange if exchange is not None else exchange_account(world)
    return dataclasses.replace(
        world, executor=DoubtfulExchange(world.executor, account, **settings)
    )
