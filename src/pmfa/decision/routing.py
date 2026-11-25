"""Routing: who decides on a gated proposal (auto, approve, or halt), and the risk score.

The risk score is the number of auto conditions that failed. Any stop
condition halts. Auto is honored only at the autonomous stage; below it, a
proposal that would have gone auto is sent to review and flagged as such, so
the stage ladder can count how often review agreed.
"""

from __future__ import annotations

from pmfa.decision.types import RoutingContext
from pmfa.records.base import Stage
from pmfa.records.decision import Proposal, Routing, RoutingTier

STAGE_BELOW_AUTONOMOUS = "stage_below_autonomous"


def auto_conditions(proposal: Proposal, context: RoutingContext) -> dict[str, bool]:
    """Each auto condition by name, true when it holds."""
    cfg = context.config
    risk_unit = cfg.caps.risk_unit_fraction * proposal.bankroll
    calibration = context.calibration
    calibrated = (
        calibration.n_resolved >= cfg.routing.calibration_min_resolved
        and calibration.calibration_error is not None
        and calibration.calibration_error < cfg.routing.calibration_error_max
    )
    return {
        "cost_under_risk_unit": proposal.cost < cfg.routing.auto_max_risk_units * risk_unit,
        "liquidity_floor": context.liquidity_dollars >= cfg.caps.liquidity_floor_dollars,
        "category_calibrated": calibrated,
        "protected_not_driving": not proposal.protected_drove_size,
        "price_stable": abs(context.price_move) <= cfg.routing.max_price_move,
    }


def stop_conditions(context: RoutingContext) -> list[str]:
    """Names of the stop conditions in force; any one halts."""
    stops = []
    if context.halted:
        stops.append("halt_flag")
    if context.risk.loss_stop_tripped:
        stops.append("daily_loss_stop")
    if context.risk.drawdown_tripped:
        stops.append("drawdown_halt")
    return stops


def route(proposal: Proposal, context: RoutingContext) -> Routing:
    """The routing tier for ``proposal``, with its score and the reasons behind it."""
    conditions = auto_conditions(proposal, context)
    failed = [name for name, held in conditions.items() if not held]
    stops = stop_conditions(context)
    if stops:
        return Routing(tier=RoutingTier.HALT, score=len(failed), reasons=stops + failed)
    if failed:
        return Routing(tier=RoutingTier.APPROVE, score=len(failed), reasons=failed)
    if context.stage is Stage.AUTONOMOUS:
        return Routing(tier=RoutingTier.AUTO, score=0, reasons=[])
    return Routing(
        tier=RoutingTier.APPROVE, would_be_auto=True, score=0, reasons=[STAGE_BELOW_AUTONOMOUS]
    )
