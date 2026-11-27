"""The gate: every limit re-verified in code before an order can be sent.

The gate is deliberately independent of sizing. It never calls sizing code:
it recomputes the shrunk probability, the fee, the edge-preserving bound, and
every exposure from the context it is handed, with its own arithmetic (the
bound is solved in closed form here, where sizing searches the ticks). A bug
in one is then unlikely to be repeated in the other.

The gate lists every failed check. It returns a verdict only: ``GateResult``
has no order field, and the proposal is a frozen record, so nothing here can
change what is sent.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from pmfa.decision.confidence import origin_rule_met
from pmfa.decision.fees import UnknownFeeType, fee, fee_rate
from pmfa.decision.ticks import is_valid_tick, snap_down
from pmfa.decision.types import GateContext
from pmfa.records.base import ONE, ZERO, Stage
from pmfa.records.decision import GateCheck, GateResult, Proposal
from pmfa.records.market import Market, Side


def gate_edge_bound(
    p_side: Decimal, market: Market, side: Side, rate: Decimal, min_edge: Decimal
) -> Decimal | None:
    """The highest valid tick ``t`` with ``p - t - rate x t x (1 - t) >= min_edge``, in closed form.

    The condition is ``rate t^2 - (1 + rate) t + (p - min_edge) >= 0``; prices
    below the smaller root qualify. The root is snapped down to a tick and the
    result verified, stepping down a tick if decimal rounding put it one too high.
    """
    slack = p_side - min_edge
    if slack <= ZERO:
        return None
    if rate == ZERO:
        root = slack
    else:
        disc = (ONE + rate) ** 2 - 4 * rate * slack
        root = ONE if disc < ZERO else 2 * slack / ((ONE + rate) + disc.sqrt())
    tick = snap_down(min(root, ONE), market, side)
    while tick is not None and p_side - tick - rate * tick * (ONE - tick) < min_edge:
        tick = snap_down(tick - Decimal("1e-12"), market, side)
    return tick


def _signed(side: Side, contracts: int) -> int:
    return contracts if side is Side.YES else -contracts


def gate(proposal: Proposal, context: GateContext) -> GateResult:
    """Approve or reject ``proposal``, naming every check that failed."""
    cfg = context.config
    caps = cfg.caps
    checks: list[GateCheck] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append(GateCheck(name=name, passed=bool(passed), detail="" if passed else detail))

    def guarded(name: str, test: Callable[[], tuple[bool, str]]) -> None:
        try:
            passed, detail = test()
        except UnknownFeeType as exc:
            passed, detail = False, str(exc)
        check(name, passed, detail)

    market, series = context.market, context.series
    check(
        "context_matches",
        market.ticker == proposal.market_ticker
        and context.book.ticker == proposal.market_ticker
        and market.event_ticker == proposal.event_ticker
        and context.series_config.ticker == series.ticker,
        "the market, book, or series in the context is not the proposal's",
    )
    check("halt", not context.halted, "the halt flag is set")
    stage_ok = context.simulated or context.stage.rank >= Stage.APPROVAL.rank
    check("stage", stage_ok, f"real orders need the approval stage; stage is {context.stage}")
    blocked = {series.ticker, proposal.event_ticker, proposal.market_ticker} & set(
        cfg.follow.blocklist
    )
    check("blocklist", not blocked, f"blocklisted: {', '.join(sorted(blocked))}")
    followed = {s.ticker for s in cfg.follow.enabled_series()}
    check("followed_series", series.ticker in followed, f"{series.ticker} is not followed")
    check("origin_rule", origin_rule_met(context.origins), "needs 1 primary or 2 secondary origins")
    check(
        "liquidity_floor",
        context.liquidity_dollars >= caps.liquidity_floor_dollars,
        f"${context.liquidity_dollars} resting, floor ${caps.liquidity_floor_dollars}",
    )
    n, limit, side = proposal.contracts, proposal.limit_price, proposal.side
    check("positive_size", n > 0, f"size is {n}")
    check(
        "contracts_per_order",
        n <= caps.max_contracts_per_order,
        f"{n} contracts, cap {caps.max_contracts_per_order}",
    )

    # Exposure after the order, at cost: contracts still held from before keep
    # their cost basis; new contracts cost the limit price; the fee is cash out.
    # The caps are fractions of the current equity, the base sizing caps on.
    bankroll = context.risk.bankroll

    in_event = [p for p in context.positions if p.event_ticker == proposal.event_ticker]
    here = [p for p in in_event if p.market_ticker == proposal.market_ticker]
    others = sum(
        (p.cost_basis for p in in_event if p.market_ticker != proposal.market_ticker), ZERO
    )

    def market_exposure() -> Decimal:
        order_fee = fee(max(n, 0), limit, series.fee_type, series.fee_multiplier)
        before = sum(p.signed for p in here)
        basis = sum((p.cost_basis for p in here), ZERO)
        after = before + _signed(side, max(n, 0))
        if before != 0 and after * before > 0 and abs(after) <= abs(before):
            return basis * abs(after) / abs(before) + order_fee  # netting a holding down
        if before != 0 and after * before > 0:
            return basis + (abs(after) - abs(before)) * limit + order_fee  # adding to it
        return abs(after) * limit + order_fee  # a new holding, or one that crossed over

    market_cap = caps.per_market_fraction * bankroll
    event_cap = caps.per_event_fraction * bankroll

    def market_test() -> tuple[bool, str]:
        exposure = market_exposure()
        return exposure <= market_cap, f"${exposure} in the market, cap ${market_cap}"

    def event_test() -> tuple[bool, str]:
        exposure = others + market_exposure()
        return exposure <= event_cap, f"${exposure} in the event, cap ${event_cap}"

    guarded("dollars_per_market", market_test)
    guarded("dollars_per_event", event_test)

    risk = context.risk
    # The loss stop is a fraction of the day-start equity, however equity moved since.
    loss_limit = caps.daily_loss_stop_fraction * risk.day_start_equity
    check(
        "daily_loss_stop",
        not risk.loss_stop_tripped and risk.day_pnl > -loss_limit,
        f"day P&L ${risk.day_pnl}, stop at -${loss_limit}",
    )
    check(
        "drawdown_halt",
        not risk.drawdown_tripped and risk.drawdown_fraction < caps.drawdown_halt_fraction,
        f"drawdown {risk.drawdown_fraction}, halt at {caps.drawdown_halt_fraction}",
    )
    check("limit_order", proposal.order_type == "limit", f"order type {proposal.order_type}")
    check(
        "immediate_or_cancel",
        proposal.time_in_force == "immediate_or_cancel",
        f"time in force {proposal.time_in_force}",
    )
    check("valid_tick", is_valid_tick(limit, market, side), f"{limit} is not a valid {side} tick")

    mid = context.book.mid_yes()
    min_edge = cfg.sizing.min_edge
    if mid is None:
        p_side = None
    else:
        k = Decimal(str(context.series_config.shrinkage))
        forecast = min(ONE, max(ZERO, Decimal(str(context.forecast_yes))))
        p_yes = k * mid + (ONE - k) * forecast
        p_side = p_yes if side is Side.YES else ONE - p_yes

    def bound_test() -> tuple[bool, str]:
        if p_side is None:
            return False, "the book has no two-sided quote"
        rate = fee_rate(series.fee_type) * series.fee_multiplier
        bound = gate_edge_bound(p_side, market, side, rate, min_edge)
        if bound is None:
            return False, "no price keeps the minimum edge"
        return limit <= bound, f"limit {limit} above the edge-preserving bound {bound}"

    def edge_test() -> tuple[bool, str]:
        if n <= 0:
            return True, ""  # a size of zero has no per-contract edge; positive_size rejects it
        if p_side is None:
            return False, "the book has no two-sided quote"
        order_fee = fee(n, limit, series.fee_type, series.fee_multiplier)
        edge = p_side - limit - order_fee / n
        return edge >= min_edge, f"edge {edge:.4f} after the rounded fee, minimum {min_edge}"

    guarded("edge_bound", bound_test)
    guarded("min_edge", edge_test)
    check(
        "not_expired",
        context.now < proposal.expires_at,
        f"expired at {proposal.expires_at.isoformat()}",
    )

    reasons = [c.name for c in checks if not c.passed]
    return GateResult(approved=not reasons, reasons=reasons, checks=checks)
