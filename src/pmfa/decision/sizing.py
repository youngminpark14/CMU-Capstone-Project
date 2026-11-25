"""Shrinkage, edge, and size for one event.

Sizing picks the one market-and-side with the best fee-adjusted edge among
those that meet the liquidity floor and are not blocklisted, computes a
quarter-Kelly target, and proposes the difference from the current holding. Everything is decimal; the
only floats are the forecast probabilities and confidence handed in, which
are converted through ``str`` so they round-trip exactly as written.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal

from pmfa.config import Config
from pmfa.decision.fees import fee, fee_rate, per_contract_fee
from pmfa.decision.ticks import snap_up, valid_ticks
from pmfa.decision.types import Candidate, SizingDecision, SizingInputs
from pmfa.records.base import ONE, ZERO, stable_id
from pmfa.records.decision import Proposal, SizingSummary
from pmfa.records.forecast import Forecast
from pmfa.records.market import BookLevel, Market, OrderBook, Position, Side

NO_LIQUIDITY = "no market meets the liquidity floor"
NO_EDGE = "best edge after fees is below the minimum"
AT_TARGET = "the holding is already at or above the target"
NO_ROOM = "caps, displayed depth, or fee rounding leave no size with the minimum edge"
BLOCKED = "every market in the event is blocklisted"


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _clamp01(value: Decimal) -> Decimal:
    return min(ONE, max(ZERO, value))


def shrink(forecast_yes: float, mid_yes: Decimal, factor: float) -> Decimal:
    """Pull the forecast toward the market: ``factor x mid + (1 - factor) x forecast``."""
    k = _dec(factor)
    return k * mid_yes + (ONE - k) * _clamp01(_dec(forecast_yes))


def side_probability(p_yes: Decimal, side: Side) -> Decimal:
    return p_yes if side is Side.YES else ONE - p_yes


def edge_at(p_side: Decimal, price: Decimal, fee_type: str, multiplier: Decimal) -> Decimal:
    """Probability of ``side`` minus the price paid minus the unrounded taker fee per contract."""
    return p_side - price - per_contract_fee(price, fee_type, multiplier)


def edge_bound(
    p_side: Decimal,
    market: Market,
    side: Side,
    fee_type: str,
    multiplier: Decimal,
    min_edge: Decimal,
) -> Decimal | None:
    """The highest valid tick on ``side`` at which the edge is still at least ``min_edge``.

    The edge falls strictly as the price rises (the fee's slope is far below 1),
    so a binary search over the ticks finds the last one that qualifies.
    """
    ticks = valid_ticks(market, side)
    lo, hi = 0, len(ticks)
    while lo < hi:
        mid = (lo + hi) // 2
        if edge_at(p_side, ticks[mid], fee_type, multiplier) >= min_edge:
            lo = mid + 1
        else:
            hi = mid
    return ticks[lo - 1] if lo > 0 else None


def _whole(level: BookLevel) -> int:
    """The whole contracts a level offers: a fractional size rounds down, as fills do."""
    return math.floor(level.contracts)


def _level_for(levels: Sequence[BookLevel], contracts: int) -> BookLevel | None:
    """The highest level an order for ``contracts`` must reach, walking cheapest first."""
    filled = 0
    for level in levels:
        filled += _whole(level)
        if filled >= contracts:
            return level
    return None


def _blocked(inputs: SizingInputs, config: Config) -> Callable[[Market], bool]:
    """Whether a market's ticker, event, or series is on the blocklist.

    Sizing skips these so a blocked market with the best edge does not take the
    proposal from the event's tradable ones; the gate still re-verifies.
    """
    blocklist = set(config.follow.blocklist)
    whole_event = inputs.series.ticker in blocklist or inputs.event_ticker in blocklist
    return lambda m: whole_event or m.ticker in blocklist or m.event_ticker in blocklist


def _position_in(positions: Sequence[Position], market_ticker: str) -> Position | None:
    held = [p for p in positions if p.market_ticker == market_ticker and p.contracts > 0]
    if len(held) > 1:
        raise ValueError(f"more than one position for market {market_ticker}")
    return held[0] if held else None


def _candidates(inputs: SizingInputs, config: Config) -> list[Candidate]:
    fee_type, multiplier = inputs.series.fee_type, inputs.series.fee_multiplier
    floor = config.caps.liquidity_floor_dollars
    blocked = _blocked(inputs, config)
    found: list[Candidate] = []
    for market in sorted(inputs.markets, key=lambda m: m.ticker):
        if blocked(market):
            continue
        forecast = inputs.forecast_yes.get(market.ticker)
        book = inputs.books.get(market.ticker)
        if forecast is None or book is None:
            continue
        mid = book.mid_yes()
        if mid is None:
            continue
        p_yes = shrink(forecast, mid, inputs.series_config.shrinkage)
        for side in (Side.YES, Side.NO):
            best = book.best_ask(side)
            if best is None or not (ZERO < best.price < ONE):
                continue
            p_side = side_probability(p_yes, side)
            liquidity = inputs.liquidity.get(market.ticker, {}).get(side, ZERO)
            found.append(
                Candidate(
                    market_ticker=market.ticker,
                    side=side,
                    ask=best.price,
                    shrunk_probability=p_side,
                    edge=edge_at(p_side, best.price, fee_type, multiplier),
                    liquidity=liquidity,
                    meets_liquidity=liquidity >= floor,
                )
            )
    return found


def kelly_contracts(
    p_side: Decimal, price: Decimal, fee_per_contract: Decimal, scale: Decimal
) -> int:
    """Whole contracts for ``scale`` (Kelly fraction x confidence x bankroll) of full Kelly.

    Full Kelly on a binary contract bought at fee-inclusive cost ``c`` is
    ``(p - c) / (1 - c)`` of bankroll; dividing the dollars by ``c`` gives contracts.
    """
    c = price + fee_per_contract
    if c <= ZERO or c >= ONE or p_side <= c or scale <= ZERO:
        return 0
    fraction = (p_side - c) / (ONE - c)
    return int((scale * fraction / c).to_integral_value(rounding=ROUND_FLOOR))


def size_event(inputs: SizingInputs, config: Config) -> SizingDecision:
    """Size one event: a ``Proposal``, or no trade with a reason.

    Raises ``UnknownFeeType`` when the series' fee type cannot be priced.
    """
    fee_type, multiplier = inputs.series.fee_type, inputs.series.fee_multiplier
    fee_rate(fee_type)  # refuse an unknown fee type before anything else
    min_edge = config.sizing.min_edge

    candidates = _candidates(inputs, config)
    eligible = [c for c in candidates if c.meets_liquidity]
    if not eligible:
        blocked = _blocked(inputs, config)
        all_blocked = bool(inputs.markets) and all(blocked(m) for m in inputs.markets)
        return SizingDecision(
            reason=BLOCKED if all_blocked else NO_LIQUIDITY, candidates=candidates
        )
    # Best edge first; ties go to the lower ticker, then YES, so the choice is deterministic.
    best = min(eligible, key=lambda c: (-c.edge, c.market_ticker, c.side is Side.NO))
    if best.edge < min_edge:
        return SizingDecision(reason=NO_EDGE, best=best, candidates=candidates)

    market = next(m for m in inputs.markets if m.ticker == best.market_ticker)
    book: OrderBook = inputs.books[market.ticker]
    side, p_side = best.side, best.shrunk_probability
    confidence = _clamp01(_dec(inputs.confidence))
    scale = config.sizing.kelly_fraction * confidence * inputs.bankroll
    target = kelly_contracts(
        p_side, best.ask, per_contract_fee(best.ask, fee_type, multiplier), scale
    )

    position = _position_in(inputs.positions, market.ticker)
    held_same = position.contracts if position and position.side is side else 0
    held_opposite = position.contracts if position and position.side is not side else 0
    # An opposite holding is covered even when the target floors to zero: the
    # other side has the better edge, so the order is at least the holding.
    if held_opposite == 0 and target <= held_same:
        return SizingDecision(reason=AT_TARGET, best=best, candidates=candidates)
    wanted = target - held_same + held_opposite

    bound = edge_bound(p_side, market, side, fee_type, multiplier, min_edge)
    if bound is None:
        return SizingDecision(reason=NO_ROOM, best=best, candidates=candidates)
    levels = [lvl for lvl in book.asks(side) if lvl.price <= bound]
    depth = sum(_whole(lvl) for lvl in levels)
    most = min(wanted, config.caps.max_contracts_per_order, depth)

    market_cap = config.caps.per_market_fraction * inputs.bankroll
    event_cap = config.caps.per_event_fraction * inputs.bankroll
    elsewhere = sum(
        (
            p.cost_basis
            for p in inputs.positions
            if p.event_ticker == inputs.event_ticker and p.market_ticker != market.ticker
        ),
        ZERO,
    )
    same_cost = position.cost_basis if position and position.side is side else ZERO

    for contracts in range(most, 0, -1):
        level = _level_for(levels, contracts)
        limit = snap_up(level.price, market, side) if level is not None else None
        if limit is None or limit > bound:
            continue
        order_fee = fee(contracts, limit, fee_type, multiplier)
        edge = p_side - limit - order_fee / contracts
        if edge < min_edge:
            continue
        # Dollars at cost in this market afterwards. Netted contracts leave the
        # book; the remaining old ones keep their cost basis; new ones cost the limit.
        netted = min(contracts, held_opposite)
        if position is not None and held_opposite:
            residual = (held_opposite - netted) * position.avg_price
        else:
            residual = same_cost
        exposure = residual + (contracts - netted) * limit + order_fee
        if exposure > market_cap or elsewhere + exposure > event_cap:
            continue
        return SizingDecision(
            proposal=_proposal(
                inputs, config, best, book, limit, bound, contracts, order_fee, edge, position
            ),
            best=best,
            candidates=candidates,
        )
    return SizingDecision(reason=NO_ROOM, best=best, candidates=candidates)


def _proposal(
    inputs: SizingInputs,
    config: Config,
    best: Candidate,
    book: OrderBook,
    limit: Decimal,
    bound: Decimal,
    contracts: int,
    order_fee: Decimal,
    edge: Decimal,
    position: Position | None,
) -> Proposal:
    side = best.side
    before = position.signed if position else 0
    after = before + (contracts if side is Side.YES else -contracts)
    forecast_yes = inputs.forecast_yes[best.market_ticker]
    mid = book.mid_yes()
    assert mid is not None  # candidates only come from two-sided books
    return Proposal(
        proposal_id=stable_id(
            "proposal", inputs.episode_id, best.market_ticker, side.value, contracts, limit
        ),
        episode_id=inputs.episode_id,
        event_ticker=inputs.event_ticker,
        market_ticker=best.market_ticker,
        side=side,
        contracts=contracts,
        limit_price=limit,
        edge_bound_price=bound,
        reference_price=best.ask,
        market_mid=mid,
        forecast_probability=forecast_yes if side is Side.YES else 1.0 - forecast_yes,
        shrunk_probability=float(best.shrunk_probability),
        edge=edge,
        fee=order_fee,
        cost=contracts * limit + order_fee,
        confidence=inputs.confidence,
        position_before=before,
        position_target=after,
        bankroll=inputs.bankroll,
        created_at=inputs.created_at,
        expires_at=inputs.created_at + timedelta(minutes=config.oversight.proposal_ttl_minutes),
        protected_drove_size=inputs.protected_drove_size,
    )


def summarize(decision: SizingDecision) -> SizingSummary:
    """What a sizing decision would trade, for the forecast search's protected slot."""
    p = decision.proposal
    if p is None:
        return SizingSummary()
    return SizingSummary(
        market_ticker=p.market_ticker, side=p.side, contracts=p.contracts, cost=p.cost
    )


def size_fn(inputs: SizingInputs, config: Config) -> Callable[[Forecast], SizingSummary]:
    """A callback for the forecast search: what sizing would trade for a given forecast.

    ``inputs`` carries the prices, holdings and bankroll; each call replaces its
    per-market probabilities and confidence with the forecast's.
    """

    def summary(forecast: Forecast) -> SizingSummary:
        trial = inputs.model_copy(
            update={
                "forecast_yes": dict(forecast.market_probabilities),
                "confidence": forecast.confidence,
            }
        )
        return summarize(size_event(trial, config))

    return summary
