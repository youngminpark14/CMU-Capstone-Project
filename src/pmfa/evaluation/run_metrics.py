"""The metrics of one backtest run, computed from its rows with the evaluation math.

Nothing here reimplements a metric: Brier scores, skill, reliability, sample
sizes and the clustered intervals come from ``metrics`` and ``bootstrap``; P&L,
the equity curve and drawdown from ``pnl``; the episode tallies and the pruning
summary from ``summary``. This module only groups the rows (by system, by
horizon, scoring pass against trading pass) and writes the result as one JSON
document, ``metrics.json``, which the report reads.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal

from pmfa.config import Config
from pmfa.evaluation.bootstrap import brier_interval, pnl_interval, skill_interval
from pmfa.evaluation.metrics import market_brier_score, reliability, sample_size
from pmfa.evaluation.pnl import equity_curve, max_drawdown, pnl_summary
from pmfa.evaluation.resolver import EpisodeAudit
from pmfa.evaluation.rows import ForecastRow, TradeRow
from pmfa.evaluation.summary import episode_tallies, pruning_summary
from pmfa.records.base import Record
from pmfa.records.decision import RoutingTier
from pmfa.records.episode import EpisodeRecord
from pmfa.records.evidence import TriggerKind

Granularity = Literal["minute", "hour", "none"]

METRICS_FILE = "metrics.json"


class OrderPricing(Record):
    """How the book an order met was priced: a one-minute candle, the hourly fallback, or nothing."""

    order_id: str
    episode_id: str
    market_ticker: str
    granularity: Granularity
    filled_contracts: Decimal
    requested_contracts: int


def _block(
    rows: Sequence[ForecastRow], config: Config, *, abstain_as_market: bool
) -> dict[str, Any]:
    ev = config.evaluation
    return {
        "sample": sample_size(rows, abstain_as_market=abstain_as_market).model_dump(mode="json"),
        "brier": brier_interval(
            rows, seed=ev.seed, samples=ev.bootstrap_samples, abstain_as_market=abstain_as_market
        ).model_dump(mode="json"),
        "market_brier": market_brier_score(rows, abstain_as_market=abstain_as_market),
        "skill": skill_interval(
            rows, seed=ev.seed, samples=ev.bootstrap_samples, abstain_as_market=abstain_as_market
        ).model_dump(mode="json"),
    }


def forecast_block(rows: Sequence[ForecastRow], config: Config) -> dict[str, Any]:
    """Scores on the answered forecasts and with abstentions scored as the market price."""
    return {
        "answered": _block(rows, config, abstain_as_market=False),
        "abstain_as_market": _block(rows, config, abstain_as_market=True),
        "n_events_in_rows": len({r.event_ticker for r in rows}),
        "n_markets_in_rows": len({r.market_ticker for r in rows}),
        "n_rows": len(rows),
    }


def compute_metrics(
    rows: Sequence[ForecastRow],
    notes: Sequence[str],
    trades: Sequence[TradeRow],
    episodes: Sequence[EpisodeRecord],
    audits: Sequence[EpisodeAudit],
    orders: Sequence[OrderPricing],
    *,
    systems: dict[str, str],
    config: Config,
) -> dict[str, Any]:
    """Every number the report shows, as plain JSON. ``notes`` is the abstention note of each row."""
    if len(notes) != len(rows):
        raise ValueError("one note per row")
    out: dict[str, Any] = {"systems": {}, "news": {}, "reliability": {}, "abstentions": {}}
    scheduled = [(r, n) for r, n in zip(rows, notes, strict=True) if r.horizon_days is not None]
    news = [(r, n) for r, n in zip(rows, notes, strict=True) if r.horizon_days is None]
    horizons = sorted(set(config.evaluation.horizons_days), reverse=True)
    bins = config.evaluation.reliability_bins
    for system, status in systems.items():
        mine = [r for r, _ in scheduled if r.system == system]
        entry: dict[str, Any] = {"status": status, "by_horizon": {}}
        if mine:
            entry["by_horizon"]["all"] = forecast_block(mine, config)
            for h in horizons:
                at_h = [r for r in mine if r.horizon_days == h]
                if at_h:
                    entry["by_horizon"][str(h)] = forecast_block(at_h, config)
            out["reliability"][system] = {
                "answered": reliability(mine, bins=bins).model_dump(mode="json"),
                "abstain_as_market": reliability(
                    mine, bins=bins, abstain_as_market=True
                ).model_dump(mode="json"),
            }
            reasons = Counter(
                _reason(n) for r, n in scheduled if r.system == system and r.probability is None
            )
            out["abstentions"][system] = dict(
                sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
            )
        out["systems"][system] = entry
        news_rows = [r for r, _ in news if r.system == system]
        if news_rows:
            out["news"][system] = forecast_block(news_rows, config)

    bankroll = config.execution.notional_bankroll
    summary = pnl_summary(trades)
    curve = equity_curve(trades, bankroll)
    drawdown = max_drawdown(curve, bankroll)
    groups = {
        "auto": [t for t in trades if t.tier is RoutingTier.AUTO],
        "approve_fill_tagged_would_be_auto": [
            t for t in trades if t.tier is RoutingTier.APPROVE and t.would_be_auto
        ],
        "approve_fill_tagged_other": [
            t for t in trades if t.tier is RoutingTier.APPROVE and not t.would_be_auto
        ],
    }
    out["pnl"] = {
        "bankroll": str(bankroll),
        "summary": summary.model_dump(mode="json"),
        "not_trading": "0",
        "interval": pnl_interval(
            trades, seed=config.evaluation.seed, samples=config.evaluation.bootstrap_samples
        ).model_dump(mode="json"),
        "max_drawdown": drawdown.model_dump(mode="json"),
        "equity_curve": [p.model_dump(mode="json") for p in curve],
        "by_routing": {
            name: {"n_fills": len(group), **pnl_summary(group).model_dump(mode="json")}
            for name, group in groups.items()
        },
    }

    scoring_eps = [e for e in episodes if e.trigger.kind is TriggerKind.SCHEDULE]
    trading_eps = [e for e in episodes if e.trigger.kind is not TriggerKind.SCHEDULE]
    out["episodes"] = {}
    for name, group in (("scoring", scoring_eps), ("trading", trading_eps)):
        tallies = episode_tallies(group).model_dump(mode="json")
        executed = [e for e in group if any(o.filled_contracts for o in e.orders)]
        tallies["executed_with_fills"] = len(executed)
        tallies["fill_tagged_executed"] = sum(e.fill_tagged for e in executed)
        tallies["fill_tagged_would_be_auto"] = sum(
            e.fill_tagged and e.routing is not None and e.routing.would_be_auto for e in executed
        )
        tallies["auto_executed"] = sum(
            e.routing is not None and e.routing.tier is RoutingTier.AUTO for e in executed
        )
        out["episodes"][name] = tallies

    out["pruning"] = {
        "summary": pruning_summary(
            (a.audit.frozen_posterior_mass, a.audit.most_likely_was_frozen) for a in audits
        ).model_dump(mode="json"),
        "episodes": [a.model_dump(mode="json") for a in audits],
    }
    out["fill_pricing"] = {
        "orders": len(orders),
        "by_granularity": dict(Counter(o.granularity for o in orders)),
        "filled_by_granularity": dict(
            Counter(o.granularity for o in orders if o.filled_contracts > 0)
        ),
        "stale_price_exits": sum(
            e.exit is not None and e.exit.value == "stale_price" for e in episodes
        ),
    }
    return out


def _reason(note: str) -> str:
    """The short reason of an abstention note: its text up to the first colon."""
    head = note.split(":", 1)[0].strip()
    return head or "no reason recorded"


__all__ = ["METRICS_FILE", "OrderPricing", "compute_metrics", "forecast_block"]
