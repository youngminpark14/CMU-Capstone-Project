"""The stage benchmarks: what the ledger must show before the agent advances a stage.

Backtest to shadow: Brier skill above zero on at least 200 backtested markets.
Shadow to approval: four shadow weeks with positive simulated P&L after fees and
drawdown under 10%. Approval to autonomous: at least 100 approved trades with
under 15% overridden. Thresholds come from ``config.stages``. Each benchmark
returns whether it is met together with the numbers behind it, and a plain
sentence for every condition that failed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from pmfa.config import StageConfig
from pmfa.records.base import Dollars, Record
from pmfa.records.decision import Review, ReviewDecision

from .metrics import brier_skill, sample_size
from .pnl import equity_curve, max_drawdown, pnl_summary
from .rows import ForecastRow, TradeRow


class BacktestBenchmark(Record):
    met: bool
    skill: float | None
    """Brier skill with abstentions scored as the market price."""
    n_markets: int
    """Markets the system forecast at least once."""
    n_events: int
    min_markets: int
    shortfalls: list[str] = []


class ShadowBenchmark(Record):
    met: bool
    weeks: float
    total_pnl: Dollars
    max_drawdown_fraction: Decimal
    max_drawdown_dollars: Dollars
    n_trades: int
    n_settled: int
    min_weeks: int
    max_drawdown_allowed: Decimal
    shortfalls: list[str] = []


class ApprovalBenchmark(Record):
    met: bool
    n_trades: int
    """Proposals a human decided in the approval stage."""
    n_overridden: int
    """Of those, the ones the human resized or rejected."""
    override_fraction: float | None
    min_trades: int
    max_override_fraction: float
    shortfalls: list[str] = []


def backtest_benchmark(rows: Sequence[ForecastRow], *, stages: StageConfig) -> BacktestBenchmark:
    """Brier skill above zero on at least ``stages.backtest_min_markets`` backtested markets.

    Skill uses the stricter variant, scoring an abstention as the market price,
    so abstaining on hard markets cannot carry the benchmark. The market count
    is the markets the system actually forecast.
    """
    skill = brier_skill(rows, abstain_as_market=True)
    size = sample_size(rows)
    shortfalls: list[str] = []
    if size.n_markets < stages.backtest_min_markets:
        shortfalls.append(
            f"{size.n_markets} backtested markets, needs {stages.backtest_min_markets}"
        )
    if skill is None or skill <= 0:
        shortfalls.append(f"Brier skill {skill} is not above zero")
    return BacktestBenchmark(
        met=not shortfalls,
        skill=skill,
        n_markets=size.n_markets,
        n_events=size.n_events,
        min_markets=stages.backtest_min_markets,
        shortfalls=shortfalls,
    )


def shadow_benchmark(
    trades: Sequence[TradeRow],
    *,
    bankroll: Decimal,
    start: datetime,
    end: datetime,
    stages: StageConfig,
) -> ShadowBenchmark:
    """``stages.shadow_min_weeks`` shadow weeks, positive P&L after fees, drawdown under the limit.

    ``start`` and ``end`` bound the shadow period; ``trades`` are its simulated
    fills. P&L is realized (settled trades only) and drawdown is taken on the
    equity curve from ``bankroll``.
    """
    if end < start:
        raise ValueError("the shadow period ends before it starts")
    summary = pnl_summary(trades)
    drawdown = max_drawdown(equity_curve(trades, bankroll), bankroll)
    elapsed = end - start
    shortfalls: list[str] = []
    if elapsed < timedelta(weeks=stages.shadow_min_weeks):
        shortfalls.append(f"{elapsed} of shadow trading, needs {stages.shadow_min_weeks} weeks")
    if summary.total <= 0:
        shortfalls.append(f"simulated P&L after fees {summary.total} is not positive")
    if drawdown.fraction >= stages.shadow_max_drawdown_fraction:
        shortfalls.append(
            f"maximum drawdown {drawdown.fraction} is not under "
            f"{stages.shadow_max_drawdown_fraction}"
        )
    return ShadowBenchmark(
        met=not shortfalls,
        weeks=elapsed / timedelta(weeks=1),
        total_pnl=summary.total,
        max_drawdown_fraction=drawdown.fraction,
        max_drawdown_dollars=drawdown.dollars,
        n_trades=summary.n_trades,
        n_settled=summary.n_settled,
        min_weeks=stages.shadow_min_weeks,
        max_drawdown_allowed=stages.shadow_max_drawdown_fraction,
        shortfalls=shortfalls,
    )


def approval_benchmark(
    reviews: Sequence[Review | ReviewDecision], *, stages: StageConfig
) -> ApprovalBenchmark:
    """At least ``stages.approval_min_trades`` reviewed trades, under the override limit.

    Every human decision in the approval stage counts as a trade; a resize or a
    reject is an override. The fraction is compared exactly, so 15 overrides in
    100 trades is not under 15%.
    """
    decisions = [r.decision if isinstance(r, Review) else r for r in reviews]
    n = len(decisions)
    overridden = sum(d is not ReviewDecision.APPROVE for d in decisions)
    limit = Decimal(str(stages.approval_max_override_fraction))
    shortfalls: list[str] = []
    if n < stages.approval_min_trades:
        shortfalls.append(f"{n} approved trades, needs {stages.approval_min_trades}")
    if n and Decimal(overridden) >= limit * n:
        shortfalls.append(f"{overridden} of {n} trades overridden, not under {limit}")
    return ApprovalBenchmark(
        met=not shortfalls,
        n_trades=n,
        n_overridden=overridden,
        override_fraction=overridden / n if n else None,
        min_trades=stages.approval_min_trades,
        max_override_fraction=stages.approval_max_override_fraction,
        shortfalls=shortfalls,
    )
