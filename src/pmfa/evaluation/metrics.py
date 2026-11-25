"""Forecast metrics: Brier score, Brier skill against the market, reliability, sample size.

Every function takes the rows of one system. A row where the system abstained
is either dropped (``abstain_as_market=False``) or scored as if the system had
forecast the market price (``abstain_as_market=True``). Dropping answers "how
good are the forecasts it made"; the market variant answers "how good is the
system as a whole", and stops a system from flattering its skill by abstaining
on the hard questions. Skill compares agent and market on exactly the same rows
in both variants.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pmfa.records.base import Record

from .rows import ForecastRow


class ScoredRow(Record):
    """A row as the metrics see it: the probability scored, the market price, the outcome."""

    event_ticker: str
    market_ticker: str
    probability: float
    market_price: float
    outcome: bool

    @property
    def error(self) -> float:
        """The squared error of the forecast."""
        return (self.probability - float(self.outcome)) ** 2

    @property
    def market_error(self) -> float:
        """The squared error of the market price on the same row."""
        return (self.market_price - float(self.outcome)) ** 2


class SampleSize(Record):
    """How much evidence a metric rests on: rows, markets, and events side by side.

    Markets within one event are correlated, so the event count is the honest
    sample size; the market count is what a naive reading would claim.
    """

    n_rows: int
    n_markets: int
    n_events: int
    n_abstained: int = 0


class ReliabilityBin(Record):
    lower: float
    upper: float
    count: int
    mean_forecast: float | None
    """``None`` for an empty bin."""
    observed_frequency: float | None
    """Share of the bin's rows that resolved YES; ``None`` for an empty bin."""


class Reliability(Record):
    bins: list[ReliabilityBin]
    n_rows: int
    ece: float | None
    """Expected calibration error; ``None`` with no rows."""


def check_one_system(rows: Iterable[ForecastRow]) -> None:
    """Raise when rows from several systems are mixed: their scores must not be pooled."""
    systems = {r.system for r in rows}
    if len(systems) > 1:
        raise ValueError(f"rows mix systems {sorted(systems)}; score one system at a time")


def scored_rows(rows: Sequence[ForecastRow], *, abstain_as_market: bool = False) -> list[ScoredRow]:
    """The rows a metric scores, after applying the abstention rule."""
    check_one_system(rows)
    out: list[ScoredRow] = []
    for r in rows:
        if r.probability is None and not abstain_as_market:
            continue
        p = r.market_price if r.probability is None else r.probability
        out.append(
            ScoredRow(
                event_ticker=r.event_ticker,
                market_ticker=r.market_ticker,
                probability=p,
                market_price=r.market_price,
                outcome=r.outcome,
            )
        )
    return out


def brier_of(scored: Sequence[ScoredRow]) -> float | None:
    """Mean squared error of the forecasts; ``None`` with no rows."""
    if not scored:
        return None
    return sum(s.error for s in scored) / len(scored)


def market_brier_of(scored: Sequence[ScoredRow]) -> float | None:
    """Mean squared error of the market prices on the same rows; ``None`` with no rows."""
    if not scored:
        return None
    return sum(s.market_error for s in scored) / len(scored)


def skill_of(scored: Sequence[ScoredRow]) -> float | None:
    """``1 - agent Brier / market Brier`` on these rows.

    Positive means the system beat the market, zero ties it, negative is worse.
    ``None`` with no rows or when the market Brier is zero (the market was
    perfect, so skill is undefined).
    """
    market = sum(s.market_error for s in scored)
    if not scored or market == 0:
        return None
    return 1.0 - sum(s.error for s in scored) / market


def brier_score(rows: Sequence[ForecastRow], *, abstain_as_market: bool = False) -> float | None:
    """The Brier score: mean of ``(probability - outcome)^2``, lower is better."""
    return brier_of(scored_rows(rows, abstain_as_market=abstain_as_market))


def market_brier_score(
    rows: Sequence[ForecastRow], *, abstain_as_market: bool = False
) -> float | None:
    """The market's Brier score on the same rows the system is scored on."""
    return market_brier_of(scored_rows(rows, abstain_as_market=abstain_as_market))


def brier_skill(rows: Sequence[ForecastRow], *, abstain_as_market: bool = False) -> float | None:
    """Brier skill against the market: ``1 - agent Brier / market Brier`` on the same rows."""
    return skill_of(scored_rows(rows, abstain_as_market=abstain_as_market))


def reliability(
    rows: Sequence[ForecastRow], *, bins: int = 10, abstain_as_market: bool = False
) -> Reliability:
    """A reliability table over ``bins`` equal-width bins of [0, 1], and the ECE.

    Bin ``i`` holds forecasts in ``[i/bins, (i+1)/bins)``; the last bin also
    holds 1.0. The expected calibration error is the count-weighted mean of
    ``|mean forecast - observed frequency|`` over non-empty bins.
    """
    if bins < 1:
        raise ValueError("reliability needs at least one bin")
    scored = scored_rows(rows, abstain_as_market=abstain_as_market)
    members: list[list[ScoredRow]] = [[] for _ in range(bins)]
    for s in scored:
        members[min(int(s.probability * bins), bins - 1)].append(s)
    table: list[ReliabilityBin] = []
    gap = 0.0
    for i, group in enumerate(members):
        mean = sum(s.probability for s in group) / len(group) if group else None
        freq = sum(s.outcome for s in group) / len(group) if group else None
        if mean is not None and freq is not None:
            gap += len(group) * abs(mean - freq)
        table.append(
            ReliabilityBin(
                lower=i / bins,
                upper=(i + 1) / bins,
                count=len(group),
                mean_forecast=mean,
                observed_frequency=freq,
            )
        )
    return Reliability(bins=table, n_rows=len(scored), ece=gap / len(scored) if scored else None)


def expected_calibration_error(
    rows: Sequence[ForecastRow], *, bins: int = 10, abstain_as_market: bool = False
) -> float | None:
    """Expected calibration error over equal-width bins; ``None`` with no rows."""
    return reliability(rows, bins=bins, abstain_as_market=abstain_as_market).ece


def sample_size(rows: Sequence[ForecastRow], *, abstain_as_market: bool = False) -> SampleSize:
    """Rows, distinct markets, and distinct events scored, with the number of abstentions.

    Counts follow the same abstention rule as the metrics, so they describe the
    rows a score actually rests on.
    """
    scored = scored_rows(rows, abstain_as_market=abstain_as_market)
    return SampleSize(
        n_rows=len(scored),
        n_markets=len({s.market_ticker for s in scored}),
        n_events=len({s.event_ticker for s in scored}),
        n_abstained=sum(r.probability is None for r in rows),
    )
