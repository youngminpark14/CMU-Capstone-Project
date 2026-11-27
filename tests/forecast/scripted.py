"""Test helpers: real event fixtures, a scripted Models stand-in, and request builders.

The scripted models answer GENERATE from a table keyed by (level id, parent claim)
and SCORE from a table keyed by scenario claim. Every call is recorded so tests
can inspect what each role was shown.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pmfa.config import Config, SeriesConfig
from pmfa.errors import BudgetExhausted
from pmfa.forecast.types import ForecastRequest
from pmfa.records.base import Record
from pmfa.records.decision import SizingSummary
from pmfa.records.episode import CallMeta, ModelResult, Usage
from pmfa.records.evidence import Origin, Passage
from pmfa.records.forecast import Forecast
from pmfa.records.market import Event, Market, MarketStatus, Side
from pmfa.records.roles import (
    TASKS,
    GenerateInput,
    GenerateOutput,
    ModelSlot,
    OptionProbability,
    Quantiles,
    RoleTask,
    ScenarioScore,
    ScenarioThought,
    ScheduledEventBrief,
    ScoreInput,
    ScoreOutput,
)

FIXTURES = Path(__file__).parent / "fixtures"
_STATUS = {
    "active": MarketStatus.OPEN,
    "initialized": MarketStatus.UNOPENED,
    "inactive": MarketStatus.PAUSED,
    "determined": MarketStatus.CLOSED,
    "finalized": MarketStatus.SETTLED,
}


def _dec(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def load_event(name: str, *, raw_subtitle: bool = False) -> Event:
    """A trimmed real event in the exchange's wire format, as a project record.

    The YES label (``yes_sub_title``) becomes ``Market.subtitle`` unless
    ``raw_subtitle`` asks for the exchange's own ``subtitle`` field.
    """
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["event"]
    markets = [
        Market(
            ticker=m["ticker"],
            event_ticker=m["event_ticker"],
            title=m["title"],
            subtitle=m.get("subtitle", "") if raw_subtitle else m.get("yes_sub_title", ""),
            status=_STATUS[m["status"]],
            open_time=datetime.fromisoformat(m["open_time"]),
            close_time=datetime.fromisoformat(m["close_time"]),
            strike_type=m.get("strike_type", ""),
            floor_strike=_dec(m.get("floor_strike")),
            cap_strike=_dec(m.get("cap_strike")),
            custom_strike=m.get("custom_strike") or {},
            rules_primary=m.get("rules_primary", ""),
            rules_secondary=m.get("rules_secondary", ""),
            result=Side(m["result"]) if m.get("result") in ("yes", "no") else None,
        )
        for m in data["markets"]
    ]
    return Event(
        event_ticker=data["event_ticker"],
        series_ticker=data["series_ticker"],
        title=data["title"],
        sub_title=data.get("sub_title", ""),
        category=data.get("category", ""),
        mutually_exclusive=data.get("mutually_exclusive", False),
        markets=markets,
    )


def series(ticker: str) -> SeriesConfig:
    found = Config().follow.for_ticker(ticker)
    assert found is not None
    return found


# --------------------------------------------------------------------------- evidence

AS_OF = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def passage(pid: str, origin: str, *, days_old: float = 2.0, source: str = "federalreserve.gov"):
    return Passage(
        passage_id=pid,
        doc_id=f"doc-{pid}",
        origin_id=origin,
        source=source,
        url=f"https://example.gov/{pid}",
        title=f"Document {pid}",
        published_at=AS_OF - timedelta(days=days_old),
        index=0,
        text=f"Text of passage {pid}.",
    )


def origin(oid: str, *, primary: bool, days_old: float = 2.0) -> Origin:
    return Origin(
        origin_id=oid,
        source="federalreserve.gov" if primary else "news.example",
        url=f"https://example.gov/{oid}",
        published_at=AS_OF - timedelta(days=days_old),
        is_primary=primary,
    )


PASSAGES = [
    passage("p1", "o1"),
    passage("p2", "o2", source="news.example"),
    passage("p3", "o3", source="news.example", days_old=40),
    passage("p_late", "o1", days_old=-1),
    passage("p_orphan", "o_unknown"),
]
ORIGINS = [
    origin("o1", primary=True),
    origin("o2", primary=False),
    origin("o3", primary=False, days_old=40),
]


# --------------------------------------------------------------------------- levels


def level(sid: str, days_after: float, title: str = "") -> ScheduledEventBrief:
    return ScheduledEventBrief(
        scheduled_event_id=sid,
        title=title or sid,
        scheduled_at=AS_OF + timedelta(days=days_after),
        is_resolution=False,
    )


def resolution(event: Event) -> ScheduledEventBrief:
    return ScheduledEventBrief(
        scheduled_event_id="resolution:" + event.event_ticker,
        title="Resolution",
        scheduled_at=event.close_time,
        is_resolution=True,
    )


def request(
    event: Event,
    space,
    levels: list[ScheduledEventBrief],
    *,
    fast_path: bool = False,
    bankroll: str = "100",
    passages: list[Passage] | None = None,
    origins: list[Origin] | None = None,
    as_of: datetime = AS_OF,
) -> ForecastRequest:
    return ForecastRequest(
        episode_id="ep-test",
        event=event,
        space=space,
        series=series(event.series_ticker),
        levels=levels,
        passages=PASSAGES if passages is None else passages,
        origins=ORIGINS if origins is None else origins,
        as_of=as_of,
        bankroll=Decimal(bankroll),
        fast_path=fast_path,
    )


# --------------------------------------------------------------------------- scripted models


def thought(
    claim: str,
    *,
    passages: tuple[str, ...] = ("p1",),
    outcome_ids: tuple[str, ...] = (),
    low: float | None = None,
    high: float | None = None,
) -> ScenarioThought:
    return ScenarioThought(
        claim=claim,
        outcome_ids=list(outcome_ids),
        value_low=low,
        value_high=high,
        passage_ids=list(passages),
        rationale=f"GENERATOR-RATIONALE for {claim}",
    )


@dataclass
class Spec:
    """How the scripted critic scores one claim."""

    probability: float
    options: dict[str, float] | None = None
    quantiles: tuple[float, float, float, float, float] | None = None
    verdict: str = "scored"
    rejection_reason: str = ""
    evidence_request: str = ""


@dataclass
class ScriptedModels:
    """A ``Models`` stand-in that answers from tables and records every call."""

    thoughts: dict[tuple[str, str | None], list[ScenarioThought]]
    specs: dict[str, Spec]
    max_calls: int | None = None
    calls: list[tuple[RoleTask, Record, ModelSlot | None]] = field(default_factory=list)
    score_hook: Callable[[ScoreInput, ScoreOutput], ScoreOutput] | None = None
    rescore_specs: dict[str, Spec] = field(default_factory=dict)
    """Specs used the second time a claim is scored (after an evidence follow-up)."""
    _seen: dict[str, int] = field(default_factory=dict)

    def call(
        self, task: RoleTask, payload: Record, *, episode_id: str, slot: ModelSlot | None = None
    ) -> ModelResult:
        if self.max_calls is not None and len(self.calls) >= self.max_calls:
            raise BudgetExhausted("scripted cap reached")
        self.calls.append((task, payload, slot))
        if task is RoleTask.GENERATE:
            assert isinstance(payload, GenerateInput)
            output: Record = self._generate(payload)
        elif task is RoleTask.SCORE:
            assert isinstance(payload, ScoreInput)
            output = self._score(payload)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected task {task}")
        spec = TASKS[task]
        return ModelResult(
            output=output,
            meta=CallMeta(
                task=task,
                slot=slot or spec.default_slot,
                role=spec.role,
                model="scripted",
                effort="low",
                source="scripted",
            ),
        )

    def usage(self, episode_id: str) -> Usage:
        return Usage(calls=len(self.calls))

    def _generate(self, payload: GenerateInput) -> GenerateOutput:
        parent = payload.path[-1].claim if payload.path else None
        return GenerateOutput(
            thoughts=self.thoughts.get((payload.level.scheduled_event_id, parent), [])
        )

    def _score(self, payload: ScoreInput) -> ScoreOutput:
        scores = []
        for s in payload.scenarios:
            seen = self._seen.get(s.claim, 0)
            self._seen[s.claim] = seen + 1
            spec = self.rescore_specs.get(s.claim) if seen else None
            spec = spec or self.specs[s.claim]
            scores.append(
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
                    rejection_reason=spec.rejection_reason,
                    evidence_request=spec.evidence_request,
                    rationale=f"critic on {s.claim}",
                )
            )
        output = ScoreOutput(scores=scores)
        return self.score_hook(payload, output) if self.score_hook else output

    def tasks(self, task: RoleTask) -> list[tuple[Record, ModelSlot | None]]:
        return [(p, s) for t, p, s in self.calls if t is task]


# --------------------------------------------------------------------------- sizing

C25 = "KXFEDDECISION-26DEC-C25"


def threshold_size_fn(
    ticker: str = C25, price: str = "0.50"
) -> Callable[[Forecast], SizingSummary]:
    """Buy 10 YES above the price, 10 NO below it: enough to see flips and size changes."""
    p0 = float(price)

    def size(forecast: Forecast) -> SizingSummary:
        p = forecast.market_probabilities[ticker]
        if p > p0:
            return SizingSummary(
                market_ticker=ticker, side=Side.YES, contracts=10, cost=Decimal(price) * 10
            )
        if p < p0:
            return SizingSummary(
                market_ticker=ticker, side=Side.NO, contracts=10, cost=(1 - Decimal(price)) * 10
            )
        return SizingSummary()

    return size
