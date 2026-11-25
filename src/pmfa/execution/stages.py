"""The stage manager: the autonomy ladder backtest -> shadow -> approval -> autonomous.

The ladder governs progression toward real-money trading, which this project
never does: it is kept as stored state and benchmarks so the record that would
justify autonomy can be shown. The demo exchange may run at the approval stage
regardless of the ladder, because its funds are mock.

A stage advances one step at a time and only when the benchmark for the next
stage is met, computed from the state store with ``pmfa.evaluation.benchmarks``:

- backtest -> shadow: Brier skill above zero on at least 200 backtested markets.
  Rows are the resolved, priced ledger entries of backtest-mode pipeline forecasts.
- shadow -> approval: four shadow weeks with positive simulated P&L after fees and
  drawdown under 10%. Trades are the fills of orders whose episodes ran in shadow
  mode, settled from the stored settlements; the period runs from the first
  shadow episode's start to now; the bankroll is ``execution.notional_bankroll``.
- approval -> autonomous: at least 100 human-reviewed trades with under 15%
  overridden. Reviews count when a human decided them at or after the time the
  current (approval) stage was entered.

Two daily loss stops within the last seven trading days demote one stage
(``check_demotion``). A day counts when the stop tripped on it and it has an
equity point after the current stage was set, so the stops that caused one
demotion are not counted again for the next.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pmfa.config import Config
from pmfa.decision import risk_state, trading_day_start
from pmfa.errors import PmfaError
from pmfa.evaluation.benchmarks import (
    ApprovalBenchmark,
    BacktestBenchmark,
    ShadowBenchmark,
    approval_benchmark,
    backtest_benchmark,
    shadow_benchmark,
)
from pmfa.evaluation.rows import ForecastRow, TradeRow
from pmfa.ports import Clock, StateStore
from pmfa.records.base import Record, RunMode, Stage
from pmfa.records.decision import Review, RoutingTier
from pmfa.records.episode import EpisodeRecord, StageState

LADDER: list[Stage] = list(Stage)


class StageNotMet(PmfaError):
    """The benchmark for the next stage is not met (or there is no next stage)."""


class StageStatus(Record):
    """The current stage and, for the next one, its benchmark and the numbers behind it."""

    stage: Stage
    reason: str = ""
    next_stage: Stage | None
    benchmark: BacktestBenchmark | ShadowBenchmark | ApprovalBenchmark | None
    """``met``, the numbers, and a sentence per unmet condition (``shortfalls``)."""

    @property
    def met(self) -> bool:
        return self.benchmark is not None and self.benchmark.met


def next_stage(stage: Stage) -> Stage | None:
    i = LADDER.index(stage)
    return LADDER[i + 1] if i + 1 < len(LADDER) else None


def previous_stage(stage: Stage) -> Stage:
    return LADDER[max(0, LADDER.index(stage) - 1)]


class StageManager:
    """Reads and moves the persisted stage. See the module docstring for the rules."""

    def __init__(self, store: StateStore, config: Config, clock: Clock) -> None:
        self.store = store
        self.config = config
        self.clock = clock

    # -- reading

    def status(self) -> StageStatus:
        state = self.store.get_stage()
        nxt = next_stage(state.stage)
        return StageStatus(
            stage=state.stage,
            reason=state.reason,
            next_stage=nxt,
            benchmark=self._benchmark(state) if nxt is not None else None,
        )

    def _benchmark(
        self, state: StageState
    ) -> BacktestBenchmark | ShadowBenchmark | ApprovalBenchmark:
        stages = self.config.stages
        if state.stage is Stage.BACKTEST:
            return backtest_benchmark(self.forecast_rows(), stages=stages)
        if state.stage is Stage.SHADOW:
            trades, start = self.shadow_trades()
            now = self.clock.now()
            return shadow_benchmark(
                trades,
                bankroll=self.config.execution.notional_bankroll,
                start=start or now,
                end=now,
                stages=stages,
            )
        return approval_benchmark(self.approval_reviews(state), stages=stages)

    def forecast_rows(self) -> list[ForecastRow]:
        """Resolved, priced backtest ledger entries of the pipeline, as forecast rows."""
        rows = []
        for e in self.store.ledger(system="pipeline"):
            if e.mode is not RunMode.BACKTEST or e.outcome is None or e.market_price is None:
                continue
            rows.append(
                ForecastRow(
                    run_id="ledger",
                    system=e.system,
                    event_ticker=e.event_ticker,
                    market_ticker=e.market_ticker,
                    category=e.category,
                    trigger_id=e.episode_id,
                    as_of=e.as_of,
                    probability=e.probability,
                    market_price=e.market_price,
                    outcome=e.outcome,
                )
            )
        return rows

    def shadow_trades(self) -> tuple[list[TradeRow], datetime | None]:
        """Fills of shadow-mode episodes as trade rows, and when the first shadow episode started."""
        settlements = {t: (side, at) for t, side, at in self.store.list_settlements()}
        trades: list[TradeRow] = []
        start: datetime | None = None
        episodes: dict[str, EpisodeRecord | None] = {}
        for order in self.store.list_orders():
            if order.episode_id not in episodes:
                episodes[order.episode_id] = self.store.get_episode(order.episode_id)
            episode = episodes[order.episode_id]
            if episode is None or episode.mode is not RunMode.SHADOW:
                continue
            if start is None or episode.started_at < start:
                start = episode.started_at
            routing = episode.routing
            for f in order.fills:
                result, settled_at = settlements.get(f.market_ticker, (None, None))
                if settled_at is not None and settled_at < f.filled_at:
                    result, settled_at = None, None
                trades.append(
                    TradeRow(
                        episode_id=order.episode_id,
                        event_ticker=episode.event_ticker or "",
                        market_ticker=f.market_ticker,
                        side=f.side,
                        contracts=f.contracts,
                        price=f.price,
                        fee=f.fee,
                        tier=routing.tier if routing is not None else RoutingTier.APPROVE,
                        would_be_auto=routing.would_be_auto if routing is not None else False,
                        filled_at=f.filled_at,
                        result=result,
                        settled_at=settled_at,
                    )
                )
        return trades, start

    def approval_reviews(self, state: StageState) -> list[Review]:
        """Human reviews decided since the current stage was entered."""
        return [
            r
            for r in self.store.list_reviews()
            if r.reviewer == "human" and (state.at is None or r.decided_at >= state.at)
        ]

    # -- moving

    def advance(self) -> StageState:
        """Move up one stage if its benchmark is met; else raise ``StageNotMet`` with the numbers."""
        status = self.status()
        if status.next_stage is None or status.benchmark is None:
            raise StageNotMet(f"{status.stage.value} is the top stage")
        if not status.benchmark.met:
            numbers = status.benchmark.model_dump(mode="json", exclude={"met", "shortfalls"})
            raise StageNotMet(
                f"cannot advance {status.stage.value} -> {status.next_stage.value}: "
                + "; ".join(status.benchmark.shortfalls)
                + f" ({numbers})"
            )
        reason = f"benchmark met for {status.next_stage.value}"
        self.store.set_stage(status.next_stage, reason, self.clock.now())
        return self.store.get_stage()

    def demote(self, reason: str) -> StageState:
        """Drop one stage (backtest stays backtest) and record why."""
        current = self.store.get_stage().stage
        self.store.set_stage(previous_stage(current), f"demoted: {reason}", self.clock.now())
        return self.store.get_stage()

    def stop_days(self) -> list[datetime]:
        """Trading-day starts, within the last seven days, on which the daily loss stop tripped
        and that have an equity point after the current stage was set."""
        now = self.clock.now()
        tz = self.config.oversight.timezone
        since = trading_day_start(now - timedelta(days=6), tz)
        stage_at = self.store.get_stage().at
        series = [(t, e) for t, e in self.store.equity_series() if t <= now]
        last_point: dict[datetime, datetime] = {}
        for t, _ in series:
            day = trading_day_start(t, tz)
            if day >= since:
                last_point[day] = t
        days = []
        for day, t in sorted(last_point.items()):
            if stage_at is not None and t <= stage_at:
                continue
            if risk_state(series, t, self.config).loss_stop_tripped:
                days.append(day)
        return days

    def check_demotion(self) -> StageState | None:
        """Demote one stage when the daily loss stop tripped on enough days this week."""
        days = self.stop_days()
        needed = self.config.stages.demote_after_daily_stops_in_week
        if len(days) < needed:
            return None
        dates = ", ".join(d.date().isoformat() for d in days)
        return self.demote(f"daily loss stop tripped on {len(days)} days in a week ({dates})")


__all__ = [
    "LADDER",
    "StageManager",
    "StageNotMet",
    "StageStatus",
    "next_stage",
    "previous_stage",
]
