"""A tiny frozen world for the backtest tests, and scripted roles that answer every task.

Two settled events, written through ``SnapshotStore`` and ``CorpusStore`` under a
temporary data directory:

- ``KXFEDDECISION-26AUG``: three markets (hold, cut 25, hike 25), closes
  2026-08-20 18:00 UTC, resolved hold (``-H0`` YES).
- ``KXCPIYOY-26AUG``: three strikes (above 2.8, 2.9, 3.0), closes 2026-08-25
  12:29 UTC, resolved 2.9 (above 2.8 YES, the others NO).

Hourly candles at constant quotes cover each market from its open to its
close. The hold market also has one-minute candles for two hours around the
first document, so that one fill is priced from a minute candle and the rest
use the hourly fallback. Three documents: Fed statement (Aug 10), CPI release
(Aug 14), Fed speech (Aug 21, after the Fed event settled).

The scripted roles are deterministic and confident: the critic and the
baseline put 0.9 on hold and most of the CPI mass between 2.8 and 3.0, so the
pipeline finds edge against the 0.60 hold price and buys YES.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pmfa.config import Config
from pmfa.models import ScriptedModels
from pmfa.records.base import Record
from pmfa.records.evidence import Document, ScheduledEvent
from pmfa.records.market import OHLC, Candle, Event, Market, MarketStatus, Series, Side
from pmfa.records.roles import (
    BaselineForecast,
    BaselineInput,
    GenerateInput,
    GenerateOutput,
    MatchDecision,
    MatchInput,
    OptionProbability,
    PassageSelection,
    PassageSelectionInput,
    Quantiles,
    QueryPlan,
    QueryPlanInput,
    RoleTask,
    ScenarioScore,
    ScenarioThought,
    ScoreInput,
    ScoreOutput,
)
from pmfa.world.evidence.common import content_hash
from pmfa.world.evidence.corpus import CorpusManifest, CorpusStore
from pmfa.world.snapshot.store import SnapshotManifest, SnapshotStore, snapshot_path

FED = "KXFEDDECISION-26AUG"
CPI = "KXCPIYOY-26AUG"
OPEN = datetime(2026, 8, 1, tzinfo=UTC)
FED_CLOSE = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
CPI_CLOSE = datetime(2026, 8, 25, 12, 29, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 1, tzinfo=UTC)
DOC1_AT = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)

FED_MARKETS = {  # suffix: (direction, amount, label, bid, ask, result)
    "H0": ("Hike", "0", "Fed maintains rate", "0.59", "0.61", Side.YES),
    "C25": ("Cut", "25", "Cut 25bps", "0.29", "0.31", Side.NO),
    "H25": ("Hike", "25", "Hike 25bps", "0.09", "0.11", Side.NO),
}
CPI_MARKETS = {  # strike: (bid, ask, result)
    "2.8": ("0.69", "0.71", Side.YES),
    "2.9": ("0.39", "0.41", Side.NO),
    "3.0": ("0.14", "0.16", Side.NO),
}
RESULTS = {
    **{f"{FED}-{k}": v[5] for k, v in FED_MARKETS.items()},
    **{f"{CPI}-T{k}": v[2] for k, v in CPI_MARKETS.items()},
}
HOLD = {"hold": 0.9, "cut_25": 0.05, "hike_25": 0.03, "hike_gt25": 0.01, "cut_gt25": 0.01}
CPI_QUANTILES = Quantiles(q05=2.82, q25=2.86, q50=2.88, q75=2.92, q95=2.98)


def make_config(data_dir: Path, **evaluation: object) -> Config:
    """Defaults, with the data under ``data_dir``, a window that starts Aug 1, and few resamples."""
    base = Config()
    ev = {"window_start": date(2026, 8, 1), "bootstrap_samples": 200, **evaluation}
    return base.model_copy(
        update={
            "paths": base.paths.model_copy(
                update={"data_dir": str(data_dir), "runs_dir": str(data_dir.parent / "runs")}
            ),
            "evaluation": base.evaluation.model_copy(update=ev),
        }
    )


def _series(ticker: str) -> Series:
    return Series(
        ticker=ticker,
        title=ticker,
        category="Economics",
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal("1"),
    )


def fed_event() -> Event:
    markets = []
    for suffix, (direction, amount, label, _, _, result) in FED_MARKETS.items():
        markets.append(
            Market(
                ticker=f"{FED}-{suffix}",
                event_ticker=FED,
                title=f"Fed decision on Aug 20, 2026: {label}",
                subtitle=label,
                status=MarketStatus.SETTLED,
                open_time=OPEN,
                close_time=FED_CLOSE,
                strike_type="custom",
                custom_strike={direction: amount},
                rules_primary=(
                    f"If the Federal Reserve does a {direction} of {amount}bps to the federal "
                    "funds rate on August 20, 2026, then the market resolves to Yes."
                ),
                result=result,
                settled_at=FED_CLOSE + timedelta(minutes=10),
            )
        )
    return Event(
        event_ticker=FED,
        series_ticker="KXFEDDECISION",
        title="Fed decision in Aug 2026? Federal funds rate",
        category="Economics",
        mutually_exclusive=True,
        markets=markets,
    )


def cpi_event() -> Event:
    markets = []
    for strike, (_, _, result) in CPI_MARKETS.items():
        markets.append(
            Market(
                ticker=f"{CPI}-T{strike}",
                event_ticker=CPI,
                title=f"CPI year over year in August 2026: above {strike}%",
                subtitle=f"Above {strike}%",
                status=MarketStatus.SETTLED,
                open_time=OPEN,
                close_time=CPI_CLOSE,
                strike_type="greater",
                floor_strike=Decimal(strike),
                rules_primary=(
                    f"If the Consumer Price Index increases by more than {strike}% in the twelve "
                    "months ending August 2026, then the market resolves to Yes."
                ),
                result=result,
                settled_at=CPI_CLOSE + timedelta(minutes=30),
            )
        )
    return Event(
        event_ticker=CPI,
        series_ticker="KXCPIYOY",
        title="CPI year over year in August 2026, consumer price index inflation",
        category="Economics",
        markets=markets,
    )


def _ohlc(value: str) -> OHLC:
    v = Decimal(value)
    return OHLC(open=v, high=v, low=v, close=v)


def _candles(
    ticker: str, bid: str, ask: str, start: datetime, end: datetime, step: timedelta, volume: str
) -> list[Candle]:
    mid = str((Decimal(bid) + Decimal(ask)) / 2)
    out, at = [], start + step
    while at <= end:
        out.append(
            Candle(
                ticker=ticker,
                end_ts=at,
                interval_minutes=int(step.total_seconds() // 60),
                yes_bid=_ohlc(bid),
                yes_ask=_ohlc(ask),
                price=_ohlc(mid),
                volume=Decimal(volume),
                open_interest=Decimal("1000"),
            )
        )
        at += step
    return out


def document(doc_id: str, source: str, kind: str, title: str, text: str, at: datetime) -> Document:
    return Document(
        doc_id=doc_id,
        source=source,
        kind=kind,
        url=f"https://{source}/{doc_id}.htm",
        title=title,
        published_at=at,
        text=text,
        content_hash=content_hash(text),
    )


DOCUMENTS = [
    document(
        "fed-statement-2026-08-10",
        "federalreserve.gov",
        "fomc_statement",
        "Federal Reserve statement on the federal funds rate",
        "The Federal Open Market Committee expects to hold the federal funds rate steady at its "
        "August decision. Inflation is easing and the Committee sees no case for a rate cut or "
        "a hike in August; the target range for the federal funds rate is maintained.",
        DOC1_AT,
    ),
    document(
        "bls-cpi-2026-08-14",
        "bls.gov",
        "cpi_release",
        "Consumer Price Index, July 2026",
        "The Consumer Price Index for All Urban Consumers rose 2.9 percent over the last twelve "
        "months. Consumer price index inflation for August is expected near 2.9 percent, above "
        "2.8 percent and not above 3.0 percent over twelve months.",
        datetime(2026, 8, 14, 12, 30, tzinfo=UTC),
    ),
    document(
        "fed-speech-2026-08-21",
        "federalreserve.gov",
        "speech",
        "Governor remarks on the federal funds rate after the August decision",
        "The Committee held the federal funds rate steady in August. I expect inflation to keep "
        "easing toward the Committee's goal.",
        datetime(2026, 8, 21, 15, 0, tzinfo=UTC),
    ),
]
CALENDAR = [
    ScheduledEvent(
        scheduled_event_id="fomc_decision:2026-08-20",
        kind="fomc_decision",
        title="FOMC meeting decision, August 2026",
        scheduled_at=FED_CLOSE,
    ),
    ScheduledEvent(
        scheduled_event_id="cpi_release:2026-08",
        kind="cpi_release",
        title="Consumer Price Index for August 2026",
        scheduled_at=CPI_CLOSE,
        period="2026-08",
    ),
]


def build_data(config: Config) -> None:
    """Write the snapshot and the corpus where ``config.paths`` says they live."""
    store = SnapshotStore(snapshot_path(config))
    store.put_series(_series("KXFEDDECISION"))
    store.put_series(_series("KXCPIYOY"))
    store.put_event(fed_event())
    store.put_event(cpi_event())
    hour = timedelta(hours=1)
    for suffix, (_, _, _, bid, ask, _) in FED_MARKETS.items():
        ticker = f"{FED}-{suffix}"
        store.put_candles(
            ticker,
            60,
            _candles(ticker, bid, ask, OPEN, FED_CLOSE, hour, "5000"),
            start=OPEN,
            end=FED_CLOSE,
        )
    for strike, (bid, ask, _) in CPI_MARKETS.items():
        ticker = f"{CPI}-T{strike}"
        store.put_candles(
            ticker,
            60,
            _candles(ticker, bid, ask, OPEN, CPI_CLOSE, hour, "5000"),
            start=OPEN,
            end=CPI_CLOSE,
        )
    # One-minute candles on the hold market around the first document.
    hold = f"{FED}-H0"
    m_start, m_end = DOC1_AT - hour, DOC1_AT + hour
    store.put_candles(
        hold,
        1,
        _candles(hold, "0.59", "0.61", m_start, m_end, timedelta(minutes=1), "100"),
        start=m_start,
        end=m_end,
    )
    store.write_manifest(
        SnapshotManifest(
            built_at=WINDOW_END,
            window_start=OPEN,
            window_end=WINDOW_END,
            series=["KXFEDDECISION", "KXCPIYOY"],
            counts=store.counts(),
            source_host="synthetic",
        )
    )
    store.close()
    CorpusStore(config.paths.corpus_dir()).write(
        DOCUMENTS,
        events=CALENDAR,
        manifest=CorpusManifest(built_at=WINDOW_END, window_start=OPEN, window_end=WINDOW_END),
    )


# --------------------------------------------------------------------------- scripted roles


def _match(payload: Record) -> Record:
    assert isinstance(payload, MatchInput)
    wanted = "cpi" if payload.trigger.source == "bls.gov" else "fed_decision"
    for c in payload.candidates:
        if c.category == wanted:
            return MatchDecision(matched=True, event_ticker=c.event_ticker, rationale="Same topic.")
    return MatchDecision(matched=False, event_ticker="", rationale="No candidate on this topic.")


def _plan(payload: Record) -> Record:
    assert isinstance(payload, QueryPlanInput)
    return QueryPlan(
        query_for=f"{payload.event.title} federal funds rate hold consumer price index",
        query_against=f"{payload.event.title} rate cut inflation above 3.0 percent",
        rationale="One query each way.",
    )


def _select(payload: Record) -> Record:
    assert isinstance(payload, PassageSelectionInput)
    return PassageSelection(
        keep_passage_ids=[p.passage_id for p in payload.passages], dropped=[], rationale="All bear."
    )


def _generate(payload: Record) -> Record:
    assert isinstance(payload, GenerateInput)
    cite = [p.passage_id for p in payload.passages[:1]]
    if not payload.level.is_resolution:
        thought = ScenarioThought(
            claim=f"{payload.level.title} happens as scheduled",
            outcome_ids=[],
            value_low=None,
            value_high=None,
            passage_ids=cite,
            rationale="Scheduled.",
        )
    elif payload.event.outcome_kind == "categorical":
        thought = ScenarioThought(
            claim="The Committee holds the rate",
            outcome_ids=["hold"],
            value_low=None,
            value_high=None,
            passage_ids=cite,
            rationale="The statement says hold.",
        )
    else:
        thought = ScenarioThought(
            claim="CPI prints between 2.8 and 3.0 percent",
            outcome_ids=[],
            value_low=2.8,
            value_high=3.0,
            passage_ids=cite,
            rationale="The release says 2.9.",
        )
    return GenerateOutput(thoughts=[thought])


def _score(payload: Record) -> Record:
    assert isinstance(payload, ScoreInput)
    ladder = payload.event.outcome_kind == "ladder"
    return ScoreOutput(
        scores=[
            ScenarioScore(
                scenario_id=s.scenario_id,
                verdict="scored",
                probability=0.9,
                option_probabilities=[]
                if ladder
                else [OptionProbability(outcome_id=k, probability=v) for k, v in HOLD.items()],
                quantiles=CPI_QUANTILES if ladder else None,
                rejection_reason="",
                evidence_request="",
                rationale="Scripted critic.",
            )
            for s in payload.scenarios
        ]
    )


def _baseline(payload: Record) -> Record:
    assert isinstance(payload, BaselineInput)
    if payload.event.outcome_kind == "ladder":
        return BaselineForecast(
            option_probabilities=[], quantiles=CPI_QUANTILES, rationale="Scripted baseline."
        )
    return BaselineForecast(
        option_probabilities=[
            OptionProbability(outcome_id=k, probability=v) for k, v in HOLD.items()
        ],
        quantiles=None,
        rationale="Scripted baseline.",
    )


def scripted(config: Config) -> ScriptedModels:
    """Scripted answers for every task the backtest asks."""
    return ScriptedModels(
        {
            RoleTask.MATCH: _match,
            RoleTask.PLAN_QUERIES: _plan,
            RoleTask.SELECT_PASSAGES: _select,
            RoleTask.GENERATE: _generate,
            RoleTask.SCORE: _score,
            RoleTask.BASELINE: _baseline,
        },
        config=config,
    )


ALL_SYSTEMS = ["market", "prompt_only", "single_pass", "single_pass_similarity", "pipeline"]
