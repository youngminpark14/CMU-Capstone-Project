"""The systems a backtest compares, other than the full pipeline.

Each produces a YES probability for every market of one event at one as-of
time, or abstains with a reason:

- ``market``: the YES mid at the as-of time, as the snapshot shows it. No model.
- ``prompt_only``: one ``RoleTask.BASELINE`` call on ``ModelSlot.BASELINE`` (the
  strongest model in the pipeline) with the event brief and no passages.
- ``single_pass``: the same call with the passages retrieved as of the as-of
  time, ranked with recency and origins (``retrieval.ranking = recency_origin``).
- ``single_pass_similarity``: the same with similarity-only ranking.

Queries for the single-pass systems are built in code from the event's title
and shared rules, so the two ranking variants retrieve over the same queries
and differ only in ranking. A model failure (``ModelOutputError``, or the
episode's own call cap) is an abstention with its reason, never a number. The
run's dollar cap (``RunBudgetExhausted``) is not caught: the run must stop.

The pipeline itself runs through the episode controller (``backtest.py``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pmfa.config import Config
from pmfa.errors import BudgetExhausted, ModelOutputError
from pmfa.evaluation.rows import ForecastRow
from pmfa.forecast import (
    OutcomeSpaceError,
    bin_probabilities,
    build_outcome_space,
    event_brief,
    market_probabilities,
)
from pmfa.forecast.tree import passage_brief
from pmfa.ports import Models
from pmfa.records.base import Record, UtcTime, stable_id
from pmfa.records.forecast import OutcomeSpace
from pmfa.records.market import Event, Side
from pmfa.records.roles import BaselineForecast, BaselineInput, EventBrief, ModelSlot, RoleTask

MARKET = "market"
PROMPT_ONLY = "prompt_only"
SINGLE_PASS = "single_pass"
SINGLE_PASS_SIMILARITY = "single_pass_similarity"
PIPELINE = "pipeline"

SYSTEMS = (MARKET, PROMPT_ONLY, SINGLE_PASS, SINGLE_PASS_SIMILARITY, PIPELINE)
"""Every system a backtest can run, in report order."""
MODEL_SYSTEMS = frozenset({PROMPT_ONLY, SINGLE_PASS, SINGLE_PASS_SIMILARITY, PIPELINE})
"""Systems that need a model."""
RANKING = {SINGLE_PASS: "recency_origin", SINGLE_PASS_SIMILARITY: "similarity"}

QUERY_RULE_WORDS = 60
"""How much of the shared rules text goes into the second retrieval query."""


class SystemForecast(Record):
    """One system's forecast for one event at one as-of time, or its abstention."""

    system: str
    event_ticker: str
    as_of: UtcTime
    market_probabilities: dict[str, float] | None
    """YES probability per market; ``None`` when the system abstained."""
    abstain_reason: str = ""
    passage_ids: list[str] = []
    episode_id: str | None = None
    calls: int = 0
    cost_usd: Decimal = Decimal("0")

    @property
    def abstained(self) -> bool:
        return self.market_probabilities is None


def market_prices(event: Event) -> dict[str, float]:
    """The YES mid of every listed market that has one, as of the event view's time."""
    out: dict[str, float] = {}
    for m in event.markets:
        mid = m.mid
        if mid is not None:
            out[m.ticker] = float(mid)
    return out


def market_forecast(event: Event, as_of: datetime) -> SystemForecast:
    """The market's own forecast: its YES mid at the as-of time."""
    return SystemForecast(
        system=MARKET,
        event_ticker=event.event_ticker,
        as_of=as_of,
        market_probabilities=market_prices(event),
    )


def baseline_queries(event: Event, brief: EventBrief) -> list[str]:
    """Two retrieval queries built in code: the event's title, and its title with the rules' opening words."""
    rules = " ".join(brief.rules.split()[:QUERY_RULE_WORDS])
    return [event.title, f"{event.title} {rules}".strip()]


def baseline_episode_id(system: str, event_ticker: str, as_of: datetime) -> str:
    """The meter's episode id for one baseline call (deterministic, so reruns line up)."""
    return stable_id("baseline", system, event_ticker, as_of.isoformat())


def outcome_distribution(
    output: BaselineForecast, space: OutcomeSpace
) -> tuple[dict[str, float] | None, str]:
    """The baseline's distribution over the outcome space, or ``None`` and what was wrong.

    Categorical: the option probabilities must name distinct, known options and
    have a positive sum; they are normalized to one and unnamed options get 0.
    Ladder: five quantiles, turned into bin probabilities by the same code the
    pipeline uses.
    """
    if space.kind == "ladder":
        if output.quantiles is None:
            return None, "the answer gave no quantiles for a ladder event"
        return bin_probabilities(space, output.quantiles), ""
    valid = set(space.outcome_ids())
    given = [op.outcome_id for op in output.option_probabilities]
    if not given:
        return None, "the answer gave no option probabilities"
    if len(set(given)) != len(given):
        return None, "the answer named an option twice"
    unknown = sorted(set(given) - valid)
    if unknown:
        return None, f"the answer named unknown options: {', '.join(unknown)}"
    raw = {op.outcome_id: max(0.0, float(op.probability)) for op in output.option_probabilities}
    total = sum(raw.values())
    if not total > 0.0:
        return None, "the option probabilities sum to zero"
    return {oid: raw.get(oid, 0.0) / total for oid in space.outcome_ids()}, ""


def baseline_forecast(
    system: str,
    event: Event,
    *,
    as_of: datetime,
    config: Config,
    models: Models,
    knowledge: Any | None = None,
) -> SystemForecast:
    """One baseline call for ``event`` (as listed at ``as_of``), converted to per-market probabilities.

    ``knowledge`` is needed for the single-pass systems; it should already carry
    the ranking the system uses (see ``CanaryGuard.with_ranking``).
    """
    if system not in (PROMPT_ONLY, SINGLE_PASS, SINGLE_PASS_SIMILARITY):
        raise ValueError(f"{system!r} is not a one-call baseline")
    eid = baseline_episode_id(system, event.event_ticker, as_of)

    def abstain(reason: str, passage_ids: list[str] | None = None) -> SystemForecast:
        usage = models.usage(eid)
        return SystemForecast(
            system=system,
            event_ticker=event.event_ticker,
            as_of=as_of,
            market_probabilities=None,
            abstain_reason=reason,
            passage_ids=passage_ids or [],
            episode_id=eid,
            calls=usage.calls,
            cost_usd=usage.cost_usd,
        )

    sc = config.follow.for_ticker(event.series_ticker)
    if sc is None:
        return abstain(f"series {event.series_ticker} is not followed")
    try:
        space = build_outcome_space(event, sc)
    except OutcomeSpaceError as exc:
        return abstain(f"no outcome space: {exc}")
    brief = event_brief(event, space, sc)
    passages = []
    if system != PROMPT_ONLY:
        if knowledge is None:
            raise ValueError(f"{system} needs the knowledge port")
        hits = knowledge.search_evidence(
            baseline_queries(event, brief),
            as_of=as_of,
            category=sc.category,
            k=config.retrieval.top_k,
        )
        passages = [h.passage for h in hits]
    passage_ids = [p.passage_id for p in passages]
    payload = BaselineInput(event=brief, passages=[passage_brief(p) for p in passages], as_of=as_of)
    try:
        result = models.call(RoleTask.BASELINE, payload, episode_id=eid, slot=ModelSlot.BASELINE)
    except ModelOutputError as exc:
        return abstain(f"model output error: {exc}", passage_ids)
    except BudgetExhausted as exc:
        return abstain(f"budget exhausted: {exc}", passage_ids)
    output = result.output
    assert isinstance(output, BaselineForecast)
    distribution, problem = outcome_distribution(output, space)
    if distribution is None:
        return abstain(f"the answer could not be converted: {problem}", passage_ids)
    usage = models.usage(eid)
    return SystemForecast(
        system=system,
        event_ticker=event.event_ticker,
        as_of=as_of,
        market_probabilities=market_probabilities(space, distribution),
        passage_ids=passage_ids,
        episode_id=eid,
        calls=usage.calls,
        cost_usd=usage.cost_usd,
    )


class RowNote(Record):
    """What the per-forecast table carries beside a ``ForecastRow``: where it came from and why it abstained."""

    episode_id: str = ""
    note: str = ""


def forecast_rows(
    forecast: SystemForecast,
    *,
    run_id: str,
    category: str,
    prices: dict[str, float],
    results: dict[str, Side],
    horizon_days: int | None = None,
    trigger_id: str | None = None,
) -> list[tuple[ForecastRow, RowNote]]:
    """One row per market that has a price at the as-of time and a final result.

    ``prices`` is the YES mid per market at the as-of time (the market's own
    forecast, and what every system is compared with); ``results`` the final
    settled results from the snapshot. A market without a price or a result
    gets no row for any system, so all systems are scored on the same markets.
    """
    rows = []
    for ticker in sorted(prices):
        result = results.get(ticker)
        if result is None:
            continue
        note = forecast.abstain_reason
        probability: float | None = None
        if forecast.market_probabilities is not None:
            probability = forecast.market_probabilities.get(ticker)
            if probability is None:
                note = "the forecast has no probability for this market"
            else:
                probability = min(1.0, max(0.0, probability))
        rows.append(
            (
                ForecastRow(
                    run_id=run_id,
                    system=forecast.system,
                    event_ticker=forecast.event_ticker,
                    market_ticker=ticker,
                    category=category,
                    horizon_days=horizon_days,
                    trigger_id=trigger_id,
                    as_of=forecast.as_of,
                    probability=probability,
                    market_price=prices[ticker],
                    outcome=result is Side.YES,
                ),
                RowNote(episode_id=forecast.episode_id or "", note=note),
            )
        )
    return rows


__all__ = [
    "MARKET",
    "MODEL_SYSTEMS",
    "PIPELINE",
    "PROMPT_ONLY",
    "RANKING",
    "SINGLE_PASS",
    "SINGLE_PASS_SIMILARITY",
    "SYSTEMS",
    "RowNote",
    "SystemForecast",
    "baseline_episode_id",
    "baseline_forecast",
    "baseline_queries",
    "forecast_rows",
    "market_forecast",
    "market_prices",
    "outcome_distribution",
]
