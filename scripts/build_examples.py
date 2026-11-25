"""Build the committed sample episodes under ``examples/episodes/``.

Run it after a change to the pipeline: ``uv run --no-sync python scripts/build_examples.py``.

Every example here is a HAND-MADE FIXTURE WITH HAND-WRITTEN MODEL RECORDS. No
Claude API key existed where they were built, so a person wrote every Monitor,
Researcher, Generator and Critic answer below. They exist to show the pipeline
working end to end; they are not model output and not evidence of skill. The
outcome of every real event was known to the author when the records were
written; the records follow the documents in each file, not the outcome.

For each example the script

1. builds a source world: real Kalshi quotes from ``data/snapshot`` and real
   Federal Reserve and BLS text from ``data/corpus`` (both git-ignored; build
   them with ``pmfa data build``), or, for the synthetic example, invented news;
2. runs the episode with ``ScriptedModels`` answering from the records below,
   through ``Recording``, so ``world.json`` holds exactly what the episode read
   and ``cassette.json`` holds the hand-written answers;
3. replays the saved files offline with ``CassettePlayer`` and writes what the
   replay produced to ``expected/``, after checking that it decided the same
   thing as the scripted run.

Without ``data/`` the committed ``world.json`` files serve as the source worlds
(a recorded world replays exactly what it recorded), so the examples can be
rebuilt after a code change anywhere. ``--from data`` or ``--from committed``
forces one; the default uses ``data/`` when it is there.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmfa.config import Config
from pmfa.examples import ReplayResult, TapeModels, replay, review_policy, run_on_world
from pmfa.models import ScriptedModels
from pmfa.records.base import Record
from pmfa.records.decision import ReasonCode, ReviewDecision
from pmfa.records.episode import EpisodeRecord, Exit
from pmfa.records.evidence import Document, Trigger, TriggerKind
from pmfa.records.market import Side
from pmfa.records.roles import (
    DroppedPassage,
    GenerateInput,
    GenerateOutput,
    MatchDecision,
    OptionProbability,
    PassageSelection,
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
    RecordedWorldFile,
    ScriptedReview,
    build_recorded_world,
    load_recorded_world,
    recorded_config,
)
from pmfa.world.recording import README_FILE, WORLD_FILE, Recording, save_example

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "episodes"
SNAPSHOT = ROOT / "data" / "snapshot" / "snapshot.sqlite"
CORPUS = ROOT / "data" / "corpus"

FED_CREDIT = (
    "Federal Reserve text is in the public domain; source: Board of Governors of the "
    "Federal Reserve System"
)
BLS_CREDIT = "BLS text is in the public domain; source: U.S. Bureau of Labor Statistics"
BOOK_NOTE = (
    "Quotes are the real Kalshi bid and ask at the as-of time from the snapshot. The snapshot "
    "has no depth data, so each order book is one level per side synthesised from one-minute "
    "candles (size: a share of the trailing hour's volume), and liquidity is a candle-volume "
    "proxy; the simulated fill meets that same book."
)
HAND_MADE = (
    "**Provenance: hand-made fixture with hand-written model records.** A person wrote every "
    "model answer in `cassette.json` (see `scripts/build_examples.py`); none is model output, "
    "and nothing here is evidence of forecasting skill."
)


# --------------------------------------------------------------------------- hand-written records


def thought(
    claim: str,
    cite: list[str],
    rationale: str,
    outcomes: tuple[str, ...] = (),
    low: float | None = None,
    high: float | None = None,
) -> ScenarioThought:
    return ScenarioThought(
        claim=claim,
        outcome_ids=list(outcomes),
        value_low=low,
        value_high=high,
        passage_ids=cite,
        rationale=rationale,
    )


def score(
    probability: float,
    rationale: str,
    options: dict[str, float] | None = None,
    quantiles: tuple[float, float, float, float, float] | None = None,
) -> ScenarioScore:
    """A critic score; the scenario id is filled in from the critic's input, by claim."""
    return ScenarioScore(
        scenario_id="",
        verdict="scored",
        probability=probability,
        option_probabilities=[
            OptionProbability(outcome_id=k, probability=v) for k, v in (options or {}).items()
        ],
        quantiles=Quantiles(
            q05=quantiles[0], q25=quantiles[1], q50=quantiles[2], q75=quantiles[3], q95=quantiles[4]
        )
        if quantiles
        else None,
        rejection_reason="",
        evidence_request="",
        rationale=rationale,
    )


def keep(ids: list[str], dropped: dict[str, str], rationale: str) -> PassageSelection:
    return PassageSelection(
        keep_passage_ids=ids,
        dropped=[DroppedPassage(passage_id=k, reason=v) for k, v in dropped.items()],
        rationale=rationale,
    )


@dataclass
class Records:
    """One example's hand-written model answers.

    ``thoughts`` is keyed by (level id, parent claim), ``None`` at the first
    level; ``scores`` by claim. A question the records do not answer is an
    authoring error and stops the build.
    """

    match: MatchDecision
    plan: QueryPlan
    selections: list[PassageSelection]
    thoughts: dict[tuple[str, str | None], list[ScenarioThought]] = field(default_factory=dict)
    scores: dict[str, ScenarioScore] = field(default_factory=dict)

    def models(self, config: Config) -> ScriptedModels:
        selections: Iterator[PassageSelection] = iter(self.selections)

        def select(_: Record) -> Record:
            return next(selections)

        def generate(payload: Record) -> Record:
            assert isinstance(payload, GenerateInput)
            parent = payload.path[-1].claim if payload.path else None
            key = (payload.level.scheduled_event_id, parent)
            if key not in self.thoughts:
                raise KeyError(f"no hand-written thoughts for level {key[0]!r} under {parent!r}")
            return GenerateOutput(thoughts=self.thoughts[key])

        def critic(payload: Record) -> Record:
            assert isinstance(payload, ScoreInput)
            scores = []
            for s in payload.scenarios:
                if s.claim not in self.scores:
                    raise KeyError(f"no hand-written score for {s.claim!r}")
                scores.append(
                    self.scores[s.claim].model_copy(update={"scenario_id": s.scenario_id})
                )
            return ScoreOutput(scores=scores)

        return ScriptedModels(
            {
                RoleTask.MATCH: lambda _: self.match,
                RoleTask.PLAN_QUERIES: lambda _: self.plan,
                RoleTask.SELECT_PASSAGES: select,
                RoleTask.GENERATE: generate,
                RoleTask.SCORE: critic,
            },
            config=config,
        )


# --------------------------------------------------------------------------- source worlds


def doc_trigger(doc_id: str, at: datetime, detail: str = "") -> Trigger:
    return Trigger(
        trigger_id=f"new-document:{doc_id}",
        kind=TriggerKind.NEW_DOCUMENT,
        at=at,
        doc_id=doc_id,
        detail=detail,
    )


def real_world(
    *,
    description: str,
    event_ticker: str,
    at: datetime,
    trigger: Trigger,
    markets: list[str] | None = None,
    documents: list[Document] | None = None,
    review: ScriptedReview | None = None,
    config_overrides: dict | None = None,
    calendar: bool = True,
) -> RecordedWorldFile:
    """A source world from the snapshot and the corpus: one event's real quotes at ``at``.

    The corpus documents and calendar go in whole (unless ``documents`` replaces
    them); the recording keeps only what the episode reads.
    """
    from pmfa.world.clocks import SimClock
    from pmfa.world.evidence.corpus import CorpusStore
    from pmfa.world.snapshot.reader import SnapshotExchange
    from pmfa.world.snapshot.store import SnapshotStore

    store = SnapshotStore(SNAPSHOT)
    try:
        exchange = SnapshotExchange(store, SimClock(at), Config())
        event = exchange.get_event(event_ticker)
        if markets is not None:
            event = event.model_copy(
                update={"markets": [m for m in event.markets if m.ticker in markets]}
            )
        tickers = [m.ticker for m in event.markets]
        corpus = CorpusStore(CORPUS)
        return RecordedWorldFile(
            description=description,
            provenance="hand-made fixture",
            trigger=trigger,
            series=[exchange.get_series(event.series_ticker)],
            events=[event],
            order_books={t: exchange.get_order_book(t) for t in tickers},
            liquidity={t: {s: exchange.liquidity_dollars(t, s) for s in Side} for t in tickers},
            documents=documents if documents is not None else corpus.documents(),
            scheduled_events=corpus.events() if calendar else [],
            stage="approval",
            review=review,
            config_overrides=config_overrides or {},
        )
    finally:
        store.close()


# --------------------------------------------------------------------------- the examples


@dataclass
class Example:
    name: str
    description: str
    expect_exit: Exit
    records: Records
    source: Callable[[], RecordedWorldFile]
    readme: Callable[[EpisodeRecord], str]
    recorded: bool = True
    """Run through ``Recording`` (world.json holds what was read, with the knowledge calls),
    or directly (world.json is the source world itself, indexed again on replay)."""


OCT = "KXFEDDECISION-26OCT"
OCT_AT = datetime(2026, 10, 7, 18, 5, tzinfo=UTC)
SEPT_MINUTES = "fed:fomcminutes20260916"
M16, M17, M19 = (f"{SEPT_MINUTES}#{n}" for n in (16, 17, 19))
JEFF6, JEFF3 = "fed:jefferson20261001a#6", "fed:jefferson20261001a#3"
STATEMENT = "fed:monetary20260916a#0"
SEP_CPI_LEVEL = "cpi_release:2026-09"
OCT_RESOLUTION = f"resolution:{OCT}"

C_HOT = (
    "The September CPI report on October 14 shows inflation still elevated, with no clear cooling"
)
C_COOL = "The September CPI report on October 14 shows inflation cooling noticeably"
HOT_HIKE = (
    "After an elevated September CPI, the Committee raises the target range 25 bp on October 28"
)
HOT_HOLD = (
    "After an elevated September CPI, the Committee holds on October 28 and leaves the next "
    "increase for December"
)
COOL_HOLD = "After a cooler September CPI, the Committee holds the target range on October 28"
COOL_HIKE = "The Committee raises the target range 25 bp on October 28 despite a cooler CPI"


def hawkish_minutes_records() -> Records:
    """The September minutes read as hawkish, modestly: a 28% chance of an October hike."""
    return Records(
        match=MatchDecision(
            matched=True,
            event_ticker=OCT,
            rationale=(
                "The minutes of the September 15-16 meeting describe the Committee's outlook "
                "for the target range, which bears directly on the October 27-28 decision."
            ),
        ),
        plan=QueryPlan(
            query_for=(
                "most participants assessed another increase in the target range for the "
                "federal funds rate would likely be appropriate"
            ),
            query_against=(
                "participants saw risks to the labor market and reasons to hold the federal "
                "funds rate steady"
            ),
            rationale="One query for a further increase, one for holding the rate steady.",
        ),
        selections=[
            keep(
                [M16, M17, JEFF6, STATEMENT],
                {
                    M19: "Implementation directive boilerplate, no view on future policy.",
                    JEFF3: "Labor-market description without a policy view.",
                    "fed:fomcminutes20260729#21": "Describes the July decision, now superseded.",
                    "fed:fomcminutes20260729#18": "Describes the July meeting, now superseded.",
                },
                "Kept the September minutes' policy discussion, the September statement, and "
                "the Vice Chair's view on future adjustments.",
            )
        ],
        thoughts={
            (SEP_CPI_LEVEL, None): [
                thought(
                    C_HOT,
                    [M16, JEFF6],
                    "Participants saw inflation risks tilted to the upside and the Vice Chair "
                    "described inflation as above target amid energy shocks.",
                ),
                thought(
                    C_COOL,
                    [M17],
                    "The minutes stress that decisions depend on incoming information, so a "
                    "cooler report is the main path to waiting.",
                ),
            ],
            (OCT_RESOLUTION, C_HOT): [
                thought(
                    HOT_HIKE,
                    [M17],
                    "Most participants expected another increase by year end.",
                    outcomes=("hike_25",),
                ),
                thought(
                    HOT_HOLD,
                    [M17, JEFF6],
                    "By year end leaves December open, and the Vice Chair wants to examine "
                    "trends first.",
                    outcomes=("hold",),
                ),
            ],
            (OCT_RESOLUTION, C_COOL): [
                thought(
                    COOL_HOLD,
                    [M17],
                    "With cooler data the open-mind language points to waiting.",
                    outcomes=("hold",),
                ),
                thought(
                    COOL_HIKE,
                    [M16],
                    "A number of participants wanted a higher path on their modal outlooks.",
                    outcomes=("hike_25",),
                ),
            ],
        },
        scores={
            C_HOT: score(
                0.7,
                "Nothing in the passages points to a quick cooling; energy shocks persist.",
                {"hike_gt25": 0.01, "hike_25": 0.3, "hold": 0.68, "cut_25": 0.01, "cut_gt25": 0.0},
            ),
            C_COOL: score(
                0.3,
                "A cooler month is possible but not suggested by the cited passages.",
                {"hike_gt25": 0.0, "hike_25": 0.14, "hold": 0.84, "cut_25": 0.02, "cut_gt25": 0.0},
            ),
            HOT_HIKE: score(
                0.33,
                "Most participants favour another increase by year end, but October and "
                "December are both open.",
                {"hike_gt25": 0.03, "hike_25": 0.95, "hold": 0.02, "cut_25": 0.0, "cut_gt25": 0.0},
            ),
            HOT_HOLD: score(
                0.67,
                "Waiting for December fits the minutes' emphasis on incoming information.",
                {"hike_gt25": 0.0, "hike_25": 0.02, "hold": 0.97, "cut_25": 0.01, "cut_gt25": 0.0},
            ),
            COOL_HOLD: score(
                0.85,
                "A cooler report makes waiting the likelier choice.",
                {"hike_gt25": 0.0, "hike_25": 0.02, "hold": 0.97, "cut_25": 0.01, "cut_gt25": 0.0},
            ),
            COOL_HIKE: score(
                0.15,
                "Some participants want a higher path regardless, so a hike is not ruled out.",
                {"hike_gt25": 0.02, "hike_25": 0.95, "hold": 0.03, "cut_25": 0.0, "cut_gt25": 0.0},
            ),
        },
    )


def hawkish_minutes_world(description: str, review: ScriptedReview) -> RecordedWorldFile:
    return real_world(
        description=description,
        event_ticker=OCT,
        at=OCT_AT,
        trigger=doc_trigger(
            SEPT_MINUTES,
            OCT_AT,
            "The minutes of the September 15-16 FOMC meeting were published at 18:00 UTC; the "
            "feed noticed them five minutes later.",
        ),
        review=review,
    )


SEP = "KXFEDDECISION-26SEP"
CPI_AT = datetime(2026, 9, 11, 12, 35, tzinfo=UTC)
AUG_CPI = "bls:cpi_release:2026-08"
CPI0, CPI1, CPI2, CPI3, CPI4 = (f"{AUG_CPI}#{n}" for n in range(5))
JULY_MIN = "fed:fomcminutes20260729"
JULY_POLICY = f"{JULY_MIN}#21"
JULY_CPI = "bls:cpi_release:2026-07"
SEP_RESOLUTION = f"resolution:{SEP}"
SEP_HIKE = "The Committee raises the target range 25 bp on September 16"
SEP_HOLD = "The Committee holds the target range on September 16"


def phantom_edge_records() -> Records:
    """A hot CPI print read as hawkish; the market had already moved, so there is no edge."""
    return Records(
        match=MatchDecision(
            matched=True,
            event_ticker=SEP,
            rationale=(
                "The August CPI report is the last inflation reading before the September "
                "15-16 meeting, so it bears on that decision."
            ),
        ),
        plan=QueryPlan(
            query_for=(
                "consumer price index increased 0.4 percent in August, all items index "
                "increased 3.4 percent; policy tightening would likely be necessary"
            ),
            query_against=(
                "index for all items less food and energy rose 2.4 percent over the year; "
                "maintaining the target range for the federal funds rate"
            ),
            rationale="One query for a hike after a hot report, one for holding.",
        ),
        selections=[
            keep(
                [CPI0, CPI1, JULY_POLICY],
                {
                    CPI4: "Component detail, no bearing beyond the headline.",
                    f"{JULY_CPI}#0": "The July report, superseded by the August one.",
                    f"{JULY_CPI}#1": "July 12-month figures, superseded by August's.",
                    f"{JULY_CPI}#4": "July component detail.",
                    f"{JULY_MIN}#20": "Balance sheet discussion.",
                },
                "Kept the August CPI headline and core figures and the July minutes' policy "
                "action, with its three dissents for a hike.",
            )
        ],
        thoughts={
            (SEP_RESOLUTION, None): [
                thought(
                    SEP_HIKE,
                    [CPI0, JULY_POLICY],
                    "A 0.4 percent monthly rise follows a July meeting where three members "
                    "already wanted a hike.",
                    outcomes=("hike_25",),
                ),
                thought(
                    SEP_HOLD,
                    [CPI1],
                    "Core inflation slowed to 2.4 percent over the year, and gasoline drove "
                    "much of the monthly rise.",
                    outcomes=("hold",),
                ),
            ]
        },
        scores={
            SEP_HIKE: score(
                0.78,
                "A hot headline print after three hike dissents makes a September increase "
                "likely, though not certain.",
                {"hike_gt25": 0.02, "hike_25": 0.96, "hold": 0.02, "cut_25": 0.0, "cut_gt25": 0.0},
            ),
            SEP_HOLD: score(
                0.22,
                "Slower core inflation leaves room to wait, which a minority would prefer.",
                {"hike_gt25": 0.0, "hike_25": 0.03, "hold": 0.96, "cut_25": 0.01, "cut_gt25": 0.0},
            ),
        },
    )


CPI_SEP = "KXCPIYOY-26SEP"
CPI_STRIKES = [f"{CPI_SEP}-T{s}" for s in ("3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8")]
CPI_RESOLUTION = f"resolution:{CPI_SEP}"
CPI_STAY = "September 12-month CPI inflation stays close to August's 3.4 percent"
CPI_UP = "Energy keeps pushing 12-month CPI inflation up, to above 3.5 percent"
CPI_DOWN = "12-month CPI inflation eases to 3.3 percent or below"


def cpi_ladder_records() -> Records:
    return Records(
        match=MatchDecision(
            matched=True,
            event_ticker=CPI_SEP,
            rationale=(
                "The August CPI report is the latest reading of the 12-month CPI change that "
                "the September event resolves on."
            ),
        ),
        plan=QueryPlan(
            query_for=(
                "all items index rose 3.4 percent for the 12 months ending August; gasoline "
                "and energy index increased"
            ),
            query_against=(
                "index for all items less food and energy rose 2.4 percent over the year; "
                "food and shelter slowed"
            ),
            rationale="One query for inflation staying high or rising, one for it easing.",
        ),
        selections=[
            keep(
                [CPI0, CPI1],
                {
                    CPI2: "Food detail, small weight in the all items index.",
                    "fed:waller20260903a#4": "A speech about PCE inflation and policy.",
                    "fed:waller20260903a#5": "A speech about policy, not CPI.",
                    "bls:employment_situation:2026-08#2": "Labor market data.",
                    "bls:employment_situation:2026-08#18": "Survey methodology.",
                    f"{JULY_MIN}#3": "Open market operations.",
                },
                "Kept the release's headline: the monthly change, the 12-month change, gasoline "
                "and core.",
            )
        ],
        thoughts={
            (CPI_RESOLUTION, None): [
                thought(
                    CPI_STAY,
                    [CPI1],
                    "The 12-month rate was 3.4 percent in both July and August.",
                    low=3.3,
                    high=3.5,
                ),
                thought(
                    CPI_UP,
                    [CPI0],
                    "Gasoline drove over a third of August's 0.4 percent monthly rise.",
                    low=3.5,
                ),
                thought(
                    CPI_DOWN,
                    [CPI1],
                    "Core inflation slowed to 2.4 percent over the year.",
                    high=3.3,
                ),
            ]
        },
        scores={
            CPI_STAY: score(
                0.5,
                "Two months at 3.4 percent make a nearby reading the single likeliest case.",
                quantiles=(3.2, 3.3, 3.4, 3.5, 3.6),
            ),
            CPI_UP: score(
                0.3,
                "Energy could keep adding to the headline, but one month is thin evidence.",
                quantiles=(3.4, 3.5, 3.6, 3.7, 3.9),
            ),
            CPI_DOWN: score(
                0.2,
                "Slowing core makes some easing possible, though energy works against it.",
                quantiles=(3.0, 3.1, 3.2, 3.3, 3.4),
            ),
        },
    )


SYNTH_AT = datetime(2026, 10, 20, 14, 25, tzinfo=UTC)
SYNTH_PASSAGE = "news:example-markets:fed-hold#0"
SYNTH_TEXT = (
    "SYNTHETIC TEST TEXT, INVENTED FOR A SOFTWARE FIXTURE; IT REPORTS NOTHING REAL. "
    "Example Wire. Two Federal Reserve officials, speaking on condition of anonymity, said "
    "they would favour holding the federal funds rate steady at the October 27-28 meeting "
    "while inflation data settle, and saw no need for a further increase in October."
)


def synthetic_news() -> list[Document]:
    """One invented Example Wire story and two verbatim syndicated copies of it."""
    title = "Fed officials said to favour holding rates in October (synthetic)"
    outlets = [
        ("example-wire.example", "news:example-wire:fed-hold", 0),
        ("example-daily.example", "news:example-daily:fed-hold", 10),
        ("example-markets.example", "news:example-markets:fed-hold", 20),
    ]
    start = SYNTH_AT - timedelta(minutes=25)
    return [
        Document(
            doc_id=doc_id,
            source=source,
            kind="news",
            url=f"https://{source}/2026/10/20/fed-hold",
            title=title,
            published_at=start + timedelta(minutes=minutes),
            text=SYNTH_TEXT,
            content_hash=content_hash(SYNTH_TEXT),
        )
        for source, doc_id, minutes in outlets
    ]


def syndicated_records() -> Records:
    return Records(
        match=MatchDecision(
            matched=True,
            event_ticker=OCT,
            rationale="The story is about the October 27-28 rate decision.",
        ),
        plan=QueryPlan(
            query_for="officials favour holding the federal funds rate steady in October",
            query_against="officials see a further increase in the federal funds rate in October",
            rationale="One query for a hold, one for a hike.",
        ),
        selections=[
            keep(
                [SYNTH_PASSAGE],
                {},
                "The story reports officials' views on the October decision.",
            )
        ],
    )


# --------------------------------------------------------------------------- readmes


def unwrap(text: str) -> str:
    """One line per paragraph and per bullet, so a README stays a handful of lines."""
    out: list[str] = []
    for block in text.strip().split("\n\n"):
        items: list[str] = []
        for line in block.splitlines():
            if line.startswith(("- ", "# ")) or not items:
                items.append(line.strip())
            else:
                items[-1] += " " + line.strip()
        out.append("\n".join(items))
    return "\n\n".join(out) + "\n"


def _trade(record: EpisodeRecord) -> str:
    p = record.proposal
    if p is None:
        return "no proposal"
    filled = sum((o.filled_contracts for o in record.orders), 0)
    return (
        f"buy {p.contracts} {p.side.value.upper()} on `{p.market_ticker}` at no more than "
        f"${p.limit_price} ({filled} filled)"
    )


def readme_hawkish(record: EpisodeRecord) -> str:
    return f"""# fed-minutes-hawkish

{HAND_MADE}

The spec's 3.1 example: hawkish Fed minutes lead to a small NO position. The minutes of the
September 15-16, 2026 FOMC meeting ("most participants assessed that another increase in the
target range ... would likely be appropriate by year end") trigger an episode on the October
2026 Fed decision. The hand-written forecast puts a 25 bp October hike above the market's
price, and the proposal is to {_trade(record)}: NO on "Fed maintains rate". The trade is
approve-tier, so the episode pauses; a scripted reviewer approves it after 60 seconds.

- Inputs: the real `{OCT}` event (all five markets) and quotes at {OCT_AT:%Y-%m-%d %H:%M} UTC, five
  minutes after the minutes were published. {BOOK_NOTE}
- Evidence: the real minutes (https://www.federalreserve.gov/monetarypolicy/fomcminutes20260916.htm),
  the September 16 statement and a speech by Vice Chair Jefferson. {FED_CREDIT}.
- Outcome: the author knew when writing the records that the Committee held in October (this NO
  position lost); the records follow the minutes, not the outcome.
"""


def readme_phantom(record: EpisodeRecord) -> str:
    return f"""# phantom-edge-no-trade

{HAND_MADE}

The spec's 2.1 case: the market already prices the news. The August 2026 CPI report (0.4
percent in the month, 3.4 percent over the year) is hawkish for the September 15-16 Fed
decision, and the hand-written forecast says so, but in the five minutes after the release the
"Hike 25bps" market moved from about 0.59 to 0.815. The forecast is close to the new price, so no
market has the 2-cent minimum edge after fees and shrinkage: exit `{record.exit.value if record.exit else "?"}`.

- Inputs: the real `{SEP}` event (all five markets) and quotes at {CPI_AT:%Y-%m-%d %H:%M} UTC. {BOOK_NOTE}
- Evidence: the real BLS release (https://www.bls.gov/news.release/archives/cpi_09112026.htm) and
  the minutes of the July 28-29 FOMC meeting
  (https://www.federalreserve.gov/monetarypolicy/fomcminutes20260729.htm), whose policy
  action records three dissents for a hike. {BLS_CREDIT}. {FED_CREDIT}.
- Outcome: the author knew when writing the records that the Committee raised the rate 25 bp in
  September; the forecast stays below the market's own hike price, so it claims no foresight.
"""


def readme_resize(record: EpisodeRecord) -> str:
    review = record.review
    size = review.contracts if review is not None else "?"
    return f"""# review-resize

{HAND_MADE}

Human review in the loop. The same real inputs as `fed-minutes-hawkish` (the September 2026
FOMC minutes and the October Fed event's quotes) produce an approve-tier proposal, so the
episode pauses with a review card (`expected/review_card.json`). The scripted reviewer resizes
it to {size} contracts with reason code `size_too_large`; the gate runs again on the smaller
order at the current book, and the smaller order executes: {_trade(record)}.

- Inputs: the real `{OCT}` event and quotes at {OCT_AT:%Y-%m-%d %H:%M} UTC. {BOOK_NOTE}
- Evidence: the real minutes (https://www.federalreserve.gov/monetarypolicy/fomcminutes20260916.htm).
  {FED_CREDIT}.
- The review is scripted in `world.json` (`review`): it arrives 90 seconds after the pause,
  inside the ten-minute expiry.
- Outcome: the author knew the October outcome (a hold) when writing the records.
"""


def readme_ladder(record: EpisodeRecord) -> str:
    rows = ""
    if record.forecast is not None:
        rows = ", ".join(
            f"{t.rsplit('-', 1)[1]} {p:.2f}"
            for t, p in record.forecast.market_probabilities.items()
        )
    return f"""# cpi-ladder

{HAND_MADE}

One forecast, every strike. The August 2026 CPI report triggers an episode on the September
2026 CPI ladder. The critic gives five quantiles of the 12-month CPI change for each of three
scenarios; code turns them into a probability for every strike, so the ladder is monotone by
construction ({rows}). The market's own mids at that moment are not monotone (the spreads
are wide), and no strike has $500 resting near the touch, so nothing is traded: exit
`{record.exit.value if record.exit else "?"}`.

- Inputs: seven of the 21 real strikes of `{CPI_SEP}` (3.2% to 3.8%) and their quotes at
  {CPI_AT:%Y-%m-%d %H:%M} UTC; the rest are left out to keep the file small. {BOOK_NOTE}
- Evidence: the real BLS release (https://www.bls.gov/news.release/archives/cpi_09112026.htm).
  {BLS_CREDIT}.
- The tree is limited to one level (`forecast.max_depth = 1` in `config_overrides`) to keep the
  hand-written records short.
- Outcome: the author knew when writing the records that September CPI came in at 3.7 percent; the
  forecast centres on August's 3.4 percent, as the release suggests, and puts little weight there.
"""


def readme_syndicated(record: EpisodeRecord) -> str:
    return f"""# single-origin-syndicated

{HAND_MADE}

The origin rule. A news-triggered trade needs one primary origin or two independent secondary
origins. Here one story from a made-up outlet, "Example Wire", is syndicated verbatim by two
other made-up outlets; all three are in `world.json`, which (unlike the other examples) is the
whole source world, re-indexed on replay. Retrieval merges the three copies into one passage,
origin resolution traces it to one secondary origin, and the episode exits `{record.exit.value if record.exit else "?"}` before any
forecast.

- The news text, the outlets and their publish times are INVENTED for this fixture (the
  `.example` domains are reserved and resolve nowhere). No real news text is copied.
- The event is the real `{OCT}` with its real quotes at {SYNTH_AT:%Y-%m-%d %H:%M} UTC. {BOOK_NOTE}
- Outcome: the author knew when writing the records that the Committee held in October; the
  episode stops before any forecast, so the records make no claim about it.
"""


EXAMPLES: list[Example] = [
    Example(
        name="fed-minutes-hawkish",
        description=(
            "Real September 2026 FOMC minutes and real October Fed-decision quotes; "
            "hand-written model records lead to a small NO position on 'Fed maintains rate'."
        ),
        expect_exit=Exit.EXECUTED,
        records=hawkish_minutes_records(),
        source=lambda: hawkish_minutes_world(
            EXAMPLES[0].description, ScriptedReview(decision=ReviewDecision.APPROVE)
        ),
        readme=readme_hawkish,
    ),
    Example(
        name="phantom-edge-no-trade",
        description=(
            "Real August 2026 CPI report and real September Fed-decision quotes taken after "
            "the market moved; the hand-written forecast finds no edge."
        ),
        expect_exit=Exit.NO_EDGE,
        records=phantom_edge_records(),
        source=lambda: real_world(
            description=EXAMPLES[1].description,
            event_ticker=SEP,
            at=CPI_AT,
            trigger=doc_trigger(
                AUG_CPI,
                CPI_AT,
                "The August CPI report was published at 12:30 UTC; the feed noticed it five "
                "minutes later.",
            ),
        ),
        readme=readme_phantom,
    ),
    Example(
        name="review-resize",
        description=(
            "Real September 2026 FOMC minutes and October Fed-decision quotes; an approve-tier "
            "proposal is resized smaller by a scripted reviewer, and the smaller order executes."
        ),
        expect_exit=Exit.EXECUTED,
        records=hawkish_minutes_records(),
        source=lambda: hawkish_minutes_world(
            EXAMPLES[2].description,
            ScriptedReview(
                decision=ReviewDecision.RESIZE,
                contracts=10,
                reason_code=ReasonCode.SIZE_TOO_LARGE,
                note="First position on this event: keep it to 10 contracts.",
                after_seconds=90,
            ),
        ),
        readme=readme_resize,
    ),
    Example(
        name="cpi-ladder",
        description=(
            "Real August 2026 CPI report and seven real strikes of the September CPI ladder; "
            "one hand-written forecast gives a monotone probability for every strike."
        ),
        expect_exit=Exit.NO_EDGE,
        records=cpi_ladder_records(),
        source=lambda: real_world(
            description=EXAMPLES[3].description,
            event_ticker=CPI_SEP,
            at=CPI_AT,
            markets=CPI_STRIKES,
            trigger=doc_trigger(
                AUG_CPI,
                CPI_AT,
                "The August CPI report was published at 12:30 UTC; the feed noticed it five "
                "minutes later.",
            ),
            config_overrides={"forecast": {"max_depth": 1}},
        ),
        readme=readme_ladder,
    ),
    Example(
        name="single-origin-syndicated",
        description=(
            "One invented 'Example Wire' story syndicated twice (synthetic text) about the "
            "real October 2026 Fed decision; one secondary origin is not enough."
        ),
        expect_exit=Exit.INSUFFICIENT_ORIGINS,
        records=syndicated_records(),
        source=lambda: real_world(
            description=EXAMPLES[4].description,
            event_ticker=OCT,
            at=SYNTH_AT,
            trigger=doc_trigger(
                "news:example-markets:fed-hold", SYNTH_AT, "A synthetic news story (invented)."
            ),
            documents=synthetic_news(),
            calendar=False,
        ),
        readme=readme_syndicated,
        recorded=False,
    ),
]


# --------------------------------------------------------------------------- building


def record_example(example: Example, source: RecordedWorldFile):
    """Run the scripted episode through ``Recording``: the recorded file, cassette, and run."""
    config = recorded_config(source)
    tape = TapeModels(example.records.models(config), config)
    world = build_recorded_world(source, models=tape, review_policy=review_policy(source))
    if not example.recorded:
        run = run_on_world(world, source)
        return source, tape.cassette(run.episode.episode_id), run
    recording = Recording(world)
    run = run_on_world(recording.world, source)
    file = recording.file(
        source.trigger, description=source.description, provenance="hand-made fixture"
    ).model_copy(update={"review": source.review})
    return file, tape.cassette(run.episode.episode_id), run


def decided(record: EpisodeRecord) -> dict:
    """What an episode decided, for comparing a scripted run with its replay."""
    return {
        "exit": record.exit,
        "exit_detail": record.exit_detail,
        "forecast": record.forecast,
        "proposal": record.proposal,
        "gate": record.gate,
        "routing": record.routing,
        "review": record.review,
        "orders": record.orders,
        "summaries": [(s.node, s.kind, s.summary) for s in record.trace],
    }


def build(example: Example, use_data: bool) -> ReplayResult:
    directory = OUT / example.name
    source = example.source() if use_data else load_recorded_world(directory / WORLD_FILE)
    file, cassette, run = record_example(example, source)
    if run.episode.exit is not example.expect_exit:
        raise SystemExit(
            f"{example.name}: expected exit {example.expect_exit.value}, got "
            f"{run.episode.exit} ({run.episode.exit_detail})"
        )
    save_example(directory, file, cassette, run.episode, run.card)
    result = replay(directory)
    if decided(result.episode) != decided(run.episode):
        raise SystemExit(f"{example.name}: the replay decided differently from the scripted run")
    save_example(directory, file, cassette, result.episode, result.card)
    (directory / README_FILE).write_text(unwrap(example.readme(result.episode)), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--from",
        dest="origin",
        choices=["auto", "data", "committed"],
        default="auto",
        help="Source worlds: the data snapshot and corpus, or the committed world.json files.",
    )
    parser.add_argument("names", nargs="*", help="Build only these examples.")
    args = parser.parse_args(argv)
    use_data = args.origin == "data" or (
        args.origin == "auto" and SNAPSHOT.is_file() and CORPUS.is_dir()
    )
    chosen = [e for e in EXAMPLES if not args.names or e.name in args.names]
    for example in chosen:
        result = build(example, use_data)
        record = result.episode
        print(
            f"{example.name:<26} {record.exit.value if record.exit else record.status.value:<21}"
            f" {len(record.trace)} steps, {record.usage.calls} model calls"
            f" ({'data' if use_data else 'committed world.json'})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
