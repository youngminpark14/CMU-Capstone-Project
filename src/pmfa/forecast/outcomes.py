"""Outcome spaces: the mutually exclusive outcomes of an event, built from its markets.

A small per-series registry decides how an event's markets become outcomes:

- **Categorical** (``KXFEDDECISION``). Five markets, one option each. Options are
  read from ``custom_strike`` (``{"Hike": "0"}``), then the ticker suffix
  (``-H0``, ``-H25``, ``-H26``, ``-C25``, ``-C26``), then the subtitle
  ("Hike 0bps", "Fed maintains rate", "Cut >25bps"). The exchange spells hold as
  a zero hike, and "more than 25" as a suffix of 26.
- **Ladder** (``KXCPIYOY``, ``KXFED``, ``KXPAYROLLS``). Markets are thresholds on
  one number. Every market's YES condition is turned into a half-open interval
  ``(a, b]`` of the *reported* number (a multiple of the series increment ``d``),
  following the rules text:

  ==================  =====================  =========================
  ``strike_type``     rules text             YES interval
  ==================  =====================  =========================
  ``greater``         "more than" / "above"  ``(floor, +inf)``  (strict)
  ``greater_or_equal`` "at least"            ``(floor - d, +inf)``
  ``less``            "below"                ``(-inf, cap - d]``  (strict)
  ``less_or_equal``   "or below"             ``(-inf, cap]``
  ``between``         "between f-c"          ``(floor - d, cap]`` (inclusive)
  ==================  =====================  =========================

  The followed series all use ``greater`` with strict rules ("increases by more
  than 3.0% ... as represented by the one-decimal place value", "greater than
  2.75%", "above -100000"). The interval ends are the cut points; outcomes are the
  bins between consecutive cut points plus the two open-ended ones, and each
  market maps to the bins inside its interval.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from decimal import Decimal

from pmfa.config import SeriesConfig
from pmfa.errors import PmfaError
from pmfa.records.forecast import Outcome, OutcomeSpace
from pmfa.records.market import Event, Market, Side
from pmfa.records.roles import EventBrief, OptionBrief


class OutcomeSpaceError(PmfaError):
    """An event's markets cannot be turned into an outcome space."""


# --------------------------------------------------------------------------- categorical

FED_OPTIONS: list[tuple[str, str]] = [
    ("hike_gt25", "Hike more than 25 bp"),
    ("hike_25", "Hike 25 bp"),
    ("hold", "Hold (rate maintained)"),
    ("cut_25", "Cut 25 bp"),
    ("cut_gt25", "Cut more than 25 bp"),
]
"""The five options of a Fed decision, in a fixed order."""


def _fed_option(direction: str, amount: str) -> str | None:
    direction = direction.strip().lower()
    amount = amount.strip().replace(" ", "").lower().removesuffix("bps").removesuffix("bp")
    if amount in {"0", "00"}:
        return "hold"
    if direction not in {"hike", "cut"}:
        return None
    if amount == "25":
        return f"{direction}_25"
    if amount in {">25", "26"}:
        return f"{direction}_gt25"
    return None


_FED_SUFFIX = re.compile(r"^(?P<dir>[HC])(?P<amt>\d+)$")
_FED_SUBTITLE = re.compile(r"^(?P<dir>hike|cut)\s*(?P<amt>>?\s*\d+)\s*bps?$", re.IGNORECASE)


def fed_option_of(market: Market) -> str:
    """The option id of one ``KXFEDDECISION`` market, from its strike fields."""
    for direction, amount in market.custom_strike.items():
        option = _fed_option(direction, amount)
        if option:
            return option
    suffix = market.ticker.rsplit("-", 1)[-1].upper()
    match = _FED_SUFFIX.match(suffix)
    if match:
        option = _fed_option("hike" if match["dir"] == "H" else "cut", match["amt"])
        if option:
            return option
    subtitle = market.subtitle.strip()
    if "maintain" in subtitle.lower():
        return "hold"
    match = _FED_SUBTITLE.match(subtitle)
    if match:
        option = _fed_option(match["dir"], match["amt"])
        if option:
            return option
    raise OutcomeSpaceError(f"cannot tell which Fed decision market {market.ticker} is")


def _fed_decision_space(event: Event, series: SeriesConfig) -> OutcomeSpace:
    market_outcomes: dict[str, list[str]] = {}
    for market in event.markets:
        option = fed_option_of(market)
        if [option] in market_outcomes.values():
            raise OutcomeSpaceError(f"two markets of {event.event_ticker} mean {option}")
        market_outcomes[market.ticker] = [option]
    return OutcomeSpace(
        event_ticker=event.event_ticker,
        kind="categorical",
        outcomes=[Outcome(outcome_id=oid, label=label) for oid, label in FED_OPTIONS],
        market_outcomes=market_outcomes,
        variable=series.variable,
        unit=series.unit,
    )


def _generic_categorical_space(event: Event, series: SeriesConfig) -> OutcomeSpace:
    """One option per market, for a categorical series with no registered parser."""
    outcomes: list[Outcome] = []
    market_outcomes: dict[str, list[str]] = {}
    for market in sorted(event.markets, key=lambda m: m.ticker):
        oid = market.ticker.removeprefix(event.event_ticker).strip("-").lower() or market.ticker
        outcomes.append(Outcome(outcome_id=oid, label=market.subtitle or market.title))
        market_outcomes[market.ticker] = [oid]
    return OutcomeSpace(
        event_ticker=event.event_ticker,
        kind="categorical",
        outcomes=outcomes,
        market_outcomes=market_outcomes,
        variable=series.variable,
        unit=series.unit,
    )


# --------------------------------------------------------------------------- ladder

Interval = tuple[Decimal | None, Decimal | None]
"""``(a, b]`` of the reported number; ``None`` is unbounded."""


def yes_interval(market: Market, increment: Decimal) -> Interval:
    """The reported values on which ``market`` resolves YES, as ``(a, b]``."""
    floor, cap, kind = market.floor_strike, market.cap_strike, market.strike_type
    if kind == "greater" and floor is not None:
        return floor, None
    if kind == "greater_or_equal" and floor is not None:
        return floor - increment, None
    if kind == "less" and cap is not None:
        return None, cap - increment
    if kind == "less_or_equal" and cap is not None:
        return None, cap
    if kind == "between" and floor is not None and cap is not None:
        return floor - increment, cap
    raise OutcomeSpaceError(
        f"market {market.ticker}: strike_type {kind!r} with floor {floor} and cap {cap} "
        "is not a ladder threshold"
    )


def _places(increment: Decimal) -> int:
    exponent = increment.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _fmt(value: Decimal, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum)
    return str(rounded) if rounded == value else str(value.normalize())


def _ladder_space(event: Event, series: SeriesConfig) -> OutcomeSpace:
    if not series.increment:
        raise OutcomeSpaceError(f"series {series.ticker} is a ladder and needs an increment")
    if not event.markets:
        raise OutcomeSpaceError(f"event {event.event_ticker} has no markets")
    increment = Decimal(str(series.increment))
    places = _places(increment)
    intervals = {m.ticker: yes_interval(m, increment) for m in event.markets}
    cuts = sorted({end for a, b in intervals.values() for end in (a, b) if end is not None})
    edges: list[Decimal | None] = [None, *cuts, None]
    unit = "%" if series.unit == "percent" else (f" {series.unit}" if series.unit else "")

    outcomes: list[Outcome] = []
    for low, high in itertools.pairwise(edges):
        if low is None and high is not None:
            oid, label = f"le_{_fmt(high, places)}", f"{_fmt(high, places)}{unit} or below"
        elif high is None and low is not None:
            oid, label = f"gt_{_fmt(low, places)}", f"above {_fmt(low, places)}{unit}"
        elif low is not None and high is not None:
            lo, hi = _fmt(low, places), _fmt(high, places)
            oid, label = f"gt_{lo}_le_{hi}", f"above {lo}{unit}, up to {hi}{unit}"
        else:  # pragma: no cover - a ladder always has at least one cut point
            raise OutcomeSpaceError("ladder without thresholds")
        outcomes.append(
            Outcome(
                outcome_id=oid,
                label=label,
                low=float(low) if low is not None else None,
                high=float(high) if high is not None else None,
            )
        )

    def inside(i: int, interval: Interval) -> bool:
        a, b = interval
        low, high = edges[i], edges[i + 1]
        above_a = a is None or (low is not None and low >= a)
        below_b = b is None or (high is not None and high <= b)
        return above_a and below_b

    market_outcomes = {
        ticker: [o.outcome_id for i, o in enumerate(outcomes) if inside(i, interval)]
        for ticker, interval in sorted(intervals.items())
    }
    return OutcomeSpace(
        event_ticker=event.event_ticker,
        kind="ladder",
        outcomes=outcomes,
        market_outcomes=market_outcomes,
        variable=series.variable,
        unit=series.unit,
        increment=series.increment,
    )


# --------------------------------------------------------------------------- registry

SpaceBuilder = Callable[[Event, SeriesConfig], OutcomeSpace]

REGISTRY: dict[str, SpaceBuilder] = {
    "KXFEDDECISION": _fed_decision_space,
    "KXCPIYOY": _ladder_space,
    "KXFED": _ladder_space,
    "KXPAYROLLS": _ladder_space,
}
"""Series ticker to outcome-space builder. Unlisted series fall back on ``outcome_kind``."""


def build_outcome_space(event: Event, series_config: SeriesConfig) -> OutcomeSpace:
    """The outcome space of ``event``, through the per-series registry."""
    builder = REGISTRY.get(event.series_ticker) or REGISTRY.get(series_config.ticker)
    if builder is None:
        builder = (
            _ladder_space if series_config.outcome_kind == "ladder" else _generic_categorical_space
        )
    space = builder(event, series_config)
    if space.kind != series_config.outcome_kind:
        raise OutcomeSpaceError(
            f"series {series_config.ticker} is configured as {series_config.outcome_kind}, "
            f"but its builder made a {space.kind} space"
        )
    return space


# --------------------------------------------------------------------------- briefs


def shared_rules(event: Event, placeholder: str) -> str:
    """The rules text the event's markets share.

    Markets of one event differ only in their option or strike. The primary rules
    are reduced to their common leading and trailing words around ``placeholder``;
    the secondary rules are listed once each.
    """
    markets = sorted(event.markets, key=lambda m: m.ticker)
    primaries = [m.rules_primary.split() for m in markets if m.rules_primary.strip()]
    parts: list[str] = []
    if primaries:
        if all(p == primaries[0] for p in primaries):
            parts.append(" ".join(primaries[0]))
        else:
            shortest = min(len(p) for p in primaries)
            prefix = 0
            while prefix < shortest and len({p[prefix] for p in primaries}) == 1:
                prefix += 1
            suffix = 0
            while suffix < shortest - prefix and len({p[-1 - suffix] for p in primaries}) == 1:
                suffix += 1
            head = primaries[0][:prefix]
            tail = primaries[0][len(primaries[0]) - suffix :] if suffix else []
            parts.append(" ".join([*head, placeholder, *tail]))
    seen: set[str] = set()
    for market in markets:
        text = market.rules_secondary.strip()
        if text and text not in seen:
            seen.add(text)
            parts.append(text)
    return "\n\n".join(parts)


def event_brief(event: Event, space: OutcomeSpace, series_config: SeriesConfig) -> EventBrief:
    """What a role sees about an event: rules, options or thresholds, and never a price."""
    if space.kind == "categorical":
        return EventBrief(
            event_ticker=event.event_ticker,
            title=event.title,
            category=series_config.category,
            closes_at=event.close_time,
            rules=shared_rules(event, "<the market's option>"),
            outcome_kind="categorical",
            options=[OptionBrief(outcome_id=o.outcome_id, label=o.label) for o in space.outcomes],
            variable=space.variable,
            unit=space.unit,
        )
    strikes = sorted(
        {
            float(s)
            for m in event.markets
            for s in (m.floor_strike, m.cap_strike)
            if s is not None and m.ticker in space.market_outcomes
        }
    )
    return EventBrief(
        event_ticker=event.event_ticker,
        title=event.title,
        category=series_config.category,
        closes_at=event.close_time,
        rules=shared_rules(event, "<the market's threshold>"),
        outcome_kind="ladder",
        variable=space.variable,
        unit=space.unit,
        thresholds=strikes,
    )


# --------------------------------------------------------------------------- realized outcomes


def outcome_from_results(space: OutcomeSpace, results: dict[str, Side]) -> str | None:
    """The realized outcome id implied by settled market results, or ``None`` if ambiguous.

    An outcome is consistent with the results when it lies in every YES market's
    outcomes and in none of the NO markets' outcomes.
    """
    candidates = set(space.outcome_ids())
    for ticker, result in results.items():
        mapped = set(space.market_outcomes.get(ticker, []))
        if ticker not in space.market_outcomes:
            continue
        candidates = candidates & mapped if result is Side.YES else candidates - mapped
    return candidates.pop() if len(candidates) == 1 else None


def ladder_outcome_for_value(space: OutcomeSpace, value: float) -> str:
    """The ladder bin ``(low, high]`` that holds a reported value."""
    for outcome in space.outcomes:
        above = outcome.low is None or value > outcome.low
        below = outcome.high is None or value <= outcome.high
        if above and below:
            return outcome.outcome_id
    raise OutcomeSpaceError(f"value {value} falls in no bin of {space.event_ticker}")
