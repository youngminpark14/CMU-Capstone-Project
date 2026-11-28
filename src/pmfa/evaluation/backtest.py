"""The backtest runner: score every system on fixed horizons, simulate trading on the news, write it all down.

``run_backtest(config, run_dir=..., systems=...)`` does, in order:

1. **Refuse a leaky window.** ``assert_window_after_cutoffs``: no run starts if
   any assigned model's listed knowledge cutoff falls on or after the window start.
2. **Write the manifest** (``manifest.json``) with the plan, before anything runs.
3. **Dry run** stops here: no model call, the plan and its dollar estimate are returned.
4. **Scoring pass.** For every scoring point (an event at 7, 3 and 1 days
   before close), every system forecasts every listed market of the event as
   of that time: the market's price, the one-call baselines, and a full pipeline
   episode from a schedule trigger. One simulated clock and one state store.
   When the pipeline runs, the outcome resolver runs before each point's
   forecasts, as before each trading episode: it writes back every result the
   exchange shows as settled at the point's as-of time, so the paper account
   and the stops read it, and the critic is given earlier forecasts resolved
   before that time, counted in whole seconds, as calibration examples. At the
   end of the run the resolver settles the rest. The market and the one-call
   baselines read no outcome.
5. **Trading pass** (pipeline only). Every corpus document in the window, in
   time order, triggers an episode on one continuous clock and a second store,
   so holdings, the ledger and the bankroll carry forward. An episode starts no
   earlier than the latest outcome of an earlier order, which can be later than
   its trigger. Before each episode the outcome resolver settles what had
   settled by then; at the end, once the last order's outcome has passed too,
   it settles the rest.
6. **Outputs** under ``run_dir``: ``manifest.json``, ``forecasts.csv``,
   ``trades.csv``, ``episodes.jsonl`` (one ``EpisodeRecord`` per line, traces
   left out, in the order the run made them: the scoring pass, then the
   trading pass), ``traces/`` (a few full episodes saved as replayable
   examples), and ``metrics.json``.

Leakage: the as-of rule lives inside the ports; every retrieval passes a
canary guard and every finished episode is scanned for the canary, and a hit
aborts the run (``LeakageDetected``). Cost: the gateway's run meter enforces
``budget.max_usd_per_run``; when it trips (``RunBudgetExhausted``) the run stops
cleanly, is marked partial, and still writes everything it ran. Models: the
real gateway when the API key is set, a ``Models`` passed in (tests), or none;
without one, the systems that need a model are recorded as not run and only
the market runs. Nothing scripted or invented ever stands in for a model.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmfa.config import Config
from pmfa.errors import RunBudgetExhausted
from pmfa.evaluation.baselines import (
    MARKET,
    MARKET_PRICE_RULE,
    MODEL_SYSTEMS,
    PIPELINE,
    RANKING,
    SYSTEMS,
    RowNote,
    SystemForecast,
    baseline_forecast,
    forecast_rows,
    market_forecast,
    market_prices,
    price_source,
)
from pmfa.evaluation.estimate import plan_systems
from pmfa.evaluation.manifest import (
    COST_CONTROLS,
    FILL_ASSUMPTIONS,
    LEAKAGE_CONTROLS,
    Plan,
    Progress,
    RunManifest,
    RunUsage,
    file_sha256,
    git_details,
    hashes_digest,
    slot_info,
    write_manifest,
)
from pmfa.evaluation.resolver import EpisodeAudit, resolve_outcomes
from pmfa.evaluation.rows import ForecastRow, TradeRow
from pmfa.evaluation.run_metrics import METRICS_FILE, OrderPricing, compute_metrics
from pmfa.evaluation.schedule import (
    Schedule,
    ScoringPoint,
    build_schedule,
    releases_without_event,
)
from pmfa.models.cassette import ModelTap
from pmfa.models.pricing import assert_window_after_cutoffs
from pmfa.ports import Clock, Models, World
from pmfa.records.episode import EpisodeRecord, Exit, ModelResult, Usage
from pmfa.records.evidence import Trigger
from pmfa.records.market import Side
from pmfa.world.backtest import (
    BacktestData,
    LeakageDetected,
    assert_episode_clean,
    backtest_config,
    build_backtest_world,
)
from pmfa.world.clocks import SimClock, SystemClock
from pmfa.world.snapshot.store import snapshot_path

FORECASTS_FILE = "forecasts.csv"
TRADES_FILE = "trades.csv"
EPISODES_FILE = "episodes.jsonl"
TRACES_DIR = "traces"
WORK_DIR = "work"

NO_KEY = "not run: no API key"
NO_TRADE_EXITS = frozenset(
    {
        Exit.NO_MATCH,
        Exit.NO_EVIDENCE,
        Exit.INSUFFICIENT_ORIGINS,
        Exit.RULES_MISMATCH,
        Exit.LOW_COVERAGE,
        Exit.NO_EDGE,
        Exit.GATE_REJECTED,
        Exit.STALE_PRICE,
    }
)

FORECAST_COLUMNS = [
    "run_id",
    "system",
    "event_ticker",
    "market_ticker",
    "category",
    "horizon_days",
    "trigger_id",
    "as_of",
    "probability",
    "market_price",
    "outcome",
    "episode_id",
    "note",
]
TRADE_COLUMNS = [
    "episode_id",
    "order_id",
    "event_ticker",
    "market_ticker",
    "side",
    "contracts",
    "price",
    "fee",
    "tier",
    "would_be_auto",
    "fill_tagged",
    "filled_at",
    "result",
    "settled_at",
    "pricing",
]


# --------------------------------------------------------------------------- helpers


def _ordered_systems(systems: Sequence[str] | None, config: Config) -> list[str]:
    chosen = list(systems) if systems is not None else list(config.evaluation.systems)
    unknown = [s for s in chosen if s not in SYSTEMS]
    if unknown:
        raise ValueError(f"unknown system(s): {', '.join(unknown)} (known: {', '.join(SYSTEMS)})")
    return [s for s in SYSTEMS if s in chosen]


def _choose_models(
    models: Models | None, config: Config, *, dry_run: bool
) -> tuple[Models | None, str]:
    """The supplied models, the live gateway when the key is set, or none.

    Before the live gateway is built, LangSmith and LangChain tracing are
    switched off for the process, whatever the environment inherited.
    """
    if models is not None:
        return models, "supplied"
    if not os.environ.get(config.models.api_key_env):
        return None, "none"
    if dry_run:
        return None, "gateway"
    from pmfa.models.gateway import Gateway
    from pmfa.world.live import disable_tracing

    # Prompts must not leave the machine through an inherited tracing switch.
    disable_tracing()
    return Gateway(config, cache_dir=config.paths.cache_dir() / "models"), "gateway"


def _statuses(
    systems: list[str], source: str, *, score: bool, trade: bool, dry_run: bool
) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in SYSTEMS:
        if s not in systems:
            out[s] = "not requested"
            continue
        if s in MODEL_SYSTEMS and source == "none":
            out[s] = NO_KEY
        elif s == PIPELINE and not (score or trade):
            out[s] = "not run: --no-score and --no-trade"
        elif s != PIPELINE and not score:
            out[s] = "not run: --no-score"
        else:
            out[s] = "would run" if dry_run else "run"
    return out


def _final_statuses(
    statuses: dict[str, str],
    running: list[str],
    progress: Progress,
    *,
    points: int,
    triggers: int,
    stopped: str,
) -> dict[str, str]:
    """What each running system got through, from the run's progress.

    ``run`` when it finished everything planned for it; ``partial: k of n
    points[, j of m triggers] (why)`` when the dollar cap stopped it part way;
    ``not run: ...`` when the cap stopped the run before it scored anything.
    ``points`` and ``triggers`` are the planned counts (``triggers`` only
    applies to the pipeline, and is 0 when the trading pass was not planned).
    """
    out = dict(statuses)
    if not stopped:
        return out
    for system in running:
        done = sum(1 for p in progress.points_done if p.rsplit("/", 1)[-1] == system)
        planned, finished = points, done
        counts = f"{done} of {points} points"
        if system == PIPELINE and triggers:
            planned += triggers
            finished += len(progress.triggers_done)
            counts += f", {len(progress.triggers_done)} of {triggers} triggers"
        if finished >= planned:
            out[system] = "run"
        elif finished == 0:
            out[system] = f"not run: stopped before it started, {stopped}"
        else:
            out[system] = f"partial: {counts} ({stopped})"
    return out


def _results(data: BacktestData, event_ticker: str) -> dict[str, Side]:
    """The final settled result of every market of the event, from the snapshot."""
    event = data.snapshot.get_event(event_ticker)
    return {m.ticker: m.result for m in event.markets if m.result is not None}


def _pipeline_forecast(episode: EpisodeRecord, event_ticker: str) -> SystemForecast:
    if episode.forecast is not None and episode.event_ticker == event_ticker:
        return SystemForecast(
            system=PIPELINE,
            event_ticker=event_ticker,
            as_of=episode.as_of,
            market_probabilities=dict(episode.forecast.market_probabilities),
            passage_ids=list(episode.passage_ids),
            episode_id=episode.episode_id,
            calls=episode.usage.calls,
            cost_usd=episode.usage.cost_usd,
        )
    exit_name = episode.exit.value if episode.exit else episode.status.value
    return SystemForecast(
        system=PIPELINE,
        event_ticker=event_ticker,
        as_of=episode.as_of,
        market_probabilities=None,
        abstain_reason=f"{exit_name}: {episode.exit_detail}".strip(),
        passage_ids=list(episode.passage_ids),
        episode_id=episode.episode_id,
        calls=episode.usage.calls,
        cost_usd=episode.usage.cost_usd,
    )


class _Run:
    """The mutable state of one run: rows, episodes, recordings, progress."""

    def __init__(self, data: BacktestData, config: Config, run_dir: Path, tap: ModelTap | None):
        self.data = data
        self.config = config
        self.run_dir = run_dir
        self.tap = tap
        self.rows: list[tuple[ForecastRow, RowNote]] = []
        self.recordings: dict[str, tuple[Any, Trigger, str]] = {}
        """Episode id -> (recording, trigger, description) for every pipeline episode."""
        self.ran: list[str] = []
        """The id of every pipeline episode in the order it ran, scoring pass first.

        It includes an episode the run's dollar cap stopped, which the store
        holds but ``exits`` does not. The stored records cannot give this order:
        two late trading episodes can share their start and as-of times, and an
        id is a hash.
        """
        self.exits: dict[str, EpisodeRecord] = {}
        """Episode id -> the episode as it stood at its exit, before the resolver added to it.

        A saved trace expects this record: a replay runs the episode to its exit
        and cannot reproduce the resolution observation appended afterwards.
        """
        self.audits: list[EpisodeAudit] = []
        self.progress = Progress()
        self.stopped = ""
        self.skipped_markets: list[str] = []
        """Markets with no price at a scoring point: no system gets a row for them there."""
        self.worlds: list[World] = []
        """The worlds the passes built, so their canary guards can be read at the end."""
        self.episodes_checked = 0
        """Finished pipeline episodes scanned for the canary."""
        self.unresolved_rows: list[str] = []
        """Priced markets left out because the snapshot holds no final result for them."""
        self.quotes: list[dict[str, Any]] = []
        """Price source, quote age and last-trade age of every market price scored (``_quote_info``)."""

    def canary_check(self, episode: EpisodeRecord) -> None:
        self.episodes_checked += 1
        assert_episode_clean(
            episode,
            canary_doc_id=self.data.canary.doc_id,
            canary_text=self.config.evaluation.canary_text,
        )

    def run_episode(self, world: World, trigger: Trigger, description: str) -> EpisodeRecord:
        """One pipeline episode on a recording of ``world``; the recording is kept for the traces."""
        from pmfa.episode import EpisodeController
        from pmfa.world.recording import Recording

        recording = Recording(world)
        controller = EpisodeController(recording.world)
        try:
            episode = controller.run(trigger)
        finally:
            controller.close()
            ran = _episode_id(world, trigger)
            if ran not in self.recordings:
                self.ran.append(ran)
            self.recordings.setdefault(ran, (recording, trigger, description))
        self.exits.setdefault(episode.episode_id, episode)
        self.canary_check(episode)
        return episode


def _leakage_checks(run: _Run, *, index_built: bool) -> dict[str, Any]:
    """What the canary actually guarded in this run: the index, retrievals and episodes checked."""
    retrievals = sum(int(getattr(w.knowledge, "checked_retrievals", 0)) for w in run.worlds)
    return {
        "index_built": index_built,
        "canary_doc_id": run.data.canary.doc_id if index_built else None,
        "canary_published_at": run.data.canary.published_at.isoformat() if index_built else None,
        "retrievals_checked": retrievals,
        "episodes_checked": run.episodes_checked,
    }


def _episode_id(world: World, trigger: Trigger) -> str:
    from pmfa.episode import episode_id_for

    return episode_id_for(world.mode, trigger)


# --------------------------------------------------------------------------- the passes


def _scoring_pass(
    run: _Run,
    schedule: Schedule,
    running: list[str],
    models: Models | None,
    run_id: str,
) -> World | None:
    """Every running system at every scoring point. Returns the scoring world (for the final resolve).

    The pass exists to score forecasts. When the pipeline runs, the outcome
    resolver runs at each point's as-of time before any system forecasts there,
    the same call the trading pass makes before each episode. The critic's
    calibration examples come from the ledger index, which compares whole
    seconds, so a result settled in the last second before a point, or at it,
    reaches the paper account at that point and the critic only at a point
    with a later as-of time. A halt stops orders only: every point is still
    forecast and scored. The pass's orders are not reported as trades; the
    trading pass is the trading simulation.
    """
    from pmfa.state.store import SqliteStore

    data, config = run.data, run.config
    points = schedule.points
    if not points or not running:
        return None
    clock = SimClock(points[0].as_of)
    store = SqliteStore(run.run_dir / WORK_DIR / "scoring.sqlite")
    needs_index = any(s != MARKET for s in running)
    knowledge = (
        data.guard(data.knowledge(run.run_dir / WORK_DIR / "index-scoring"))
        if needs_index
        else None
    )
    world = build_backtest_world(
        data,
        config,
        clock=clock,
        store=store,
        models=models if models is not None else _NoModels(),
        knowledge=knowledge if knowledge is not None else data.guard(_NoKnowledge()),
    )
    run.worlds.append(world)
    # The daily loss stop and the drawdown halt measure from the equity points, so the first
    # one is the bankroll at the window's start, written before any order (as in the trading pass).
    world.executor.mark(schedule.window_start)  # type: ignore[attr-defined]
    pending = [(p, s) for p in points for s in running]
    done = 0
    for point in points:
        clock.set(point.as_of)
        if PIPELINE in running:
            # Write back what has settled by now before any system forecasts here,
            # as the trading pass does before each episode.
            run.audits.extend(resolve_outcomes(world, now=clock.now()).audits)
        event = world.exchange.get_event(point.event_ticker)
        prices = market_prices(event)
        results = _results(data, point.event_ticker)
        for m in event.markets:
            if m.ticker not in prices:
                run.skipped_markets.append(
                    f"{point.point_id} {m.ticker}: no price at the as-of time (no two-sided "
                    "quote and no trade)"
                )
            elif m.ticker in results:
                run.quotes.append(_quote_info(data, m, point.as_of))
            else:
                run.unresolved_rows.append(
                    f"{point.point_id} {m.ticker}: no final result in the snapshot"
                )
        for system in running:
            forecast = _forecast(run, world, point, system, event)
            if forecast is None:
                run.progress.points_not_run.extend(f"{p.point_id}/{s}" for p, s in pending[done:])
                return world
            run.rows.extend(
                forecast_rows(
                    forecast,
                    run_id=run_id,
                    category=point.category,
                    prices=prices,
                    results=results,
                    horizon_days=point.horizon_days,
                )
            )
            run.progress.points_done.append(f"{point.point_id}/{system}")
            done += 1
    return world


def _quote_info(data: BacktestData, market: Any, as_of: datetime) -> dict[str, Any]:
    """Where a market's price came from, the age of the quote, and the age of the last trade.

    The quote's age is the time since the latest candle at or before the as-of
    time, that is since the quote or a trade last changed (candles exist only
    for minutes with a change); a standing quote that has not moved is still
    the quote. The last trade's age is reported apart, for the prices that use it.
    """
    source = price_source(market)

    def age(traded: bool) -> float | None:
        ends = [
            c.end_ts
            for c in (
                data.snapshot.latest_candle(market.ticker, 1, until=as_of, traded=traded),
                data.snapshot.latest_candle(market.ticker, 60, until=as_of, traded=traded),
            )
            if c is not None
        ]
        return (as_of - max(ends)).total_seconds() / 3600 if ends else None

    return {
        "source": source,
        "quote_age_hours": age(False),
        "trade_age_hours": age(True) if source.startswith("last_trade") else None,
    }


def _quote_summary(quotes: list[dict[str, Any]]) -> dict[str, Any] | None:
    import statistics

    if not quotes:
        return None
    quote_ages = sorted(q["quote_age_hours"] for q in quotes if q["quote_age_hours"] is not None)
    trade_ages = sorted(q["trade_age_hours"] for q in quotes if q["trade_age_hours"] is not None)

    def spread(ages: list[float]) -> dict[str, float | None]:
        return {
            "median": statistics.median(ages) if ages else None,
            "max": ages[-1] if ages else None,
            "share_older_than_24h": sum(a > 24 for a in ages) / len(ages) if ages else None,
        }

    return {
        "n": len(quotes),
        "rule": MARKET_PRICE_RULE,
        "from_mid": sum(q["source"] == "mid" for q in quotes),
        "from_last_trade": sum(q["source"].startswith("last_trade") for q in quotes),
        "last_trade_clamped": sum(q["source"] == "last_trade_clamped" for q in quotes),
        "quote_age_hours": spread(quote_ages),
        "last_trade_age_hours": spread(trade_ages),
    }


def _forecast(
    run: _Run, world: World, point: ScoringPoint, system: str, event: Any
) -> SystemForecast | None:
    """One system's forecast at one point; ``None`` when the run's dollar cap stopped the run."""
    try:
        if system == MARKET:
            return market_forecast(event, point.as_of)
        if system == PIPELINE:
            episode = run.run_episode(
                world,
                point.trigger(),
                f"Backtest scoring episode: {point.event_ticker}, {point.horizon_days} day(s) "
                "before close, on the frozen snapshot and corpus.",
            )
            return _pipeline_forecast(episode, point.event_ticker)
        knowledge = world.knowledge.with_ranking(RANKING[system]) if system in RANKING else None  # type: ignore[attr-defined]
        return baseline_forecast(
            system,
            event,
            as_of=point.as_of,
            config=world.config,
            models=world.models,
            knowledge=knowledge,
        )
    except RunBudgetExhausted as exc:
        run.stopped = f"the run's dollar cap was reached at {point.point_id}/{system}: {exc}"
        return None


def _trading_pass(run: _Run, schedule: Schedule, models: Models, run_id: str) -> World | None:
    """The news-triggered trading simulation on one continuous clock and store.

    Episodes run one at a time. Each starts at the latest of its trigger's
    time, the clock's time, and the latest order outcome in the store (the end
    of the candle that priced an earlier order), so it never starts before an
    earlier order's outcome existed. An episode that starts later than its
    trigger runs with that later time as its as-of time; ``_late_starts``
    counts them. The clock is not moved after an episode: the forecast rows
    below read the market prices at the episode's own as-of time.
    """
    from pmfa.state.store import SqliteStore

    data, config = run.data, run.config
    triggers = schedule.triggers
    if not triggers:
        return None
    clock = SimClock(schedule.window_start)
    store = SqliteStore(run.run_dir / WORK_DIR / "trading.sqlite")
    knowledge = data.guard(data.knowledge(run.run_dir / WORK_DIR / "index-trading"))
    world = build_backtest_world(
        data, config, clock=clock, store=store, models=models, knowledge=knowledge
    )
    run.worlds.append(world)
    world.executor.mark(schedule.window_start)  # type: ignore[attr-defined]
    for i, trigger in enumerate(triggers):
        outcomes = [o.outcome_at for o in store.list_orders() if o.outcome_at is not None]
        start = max([trigger.at, clock.now(), *outcomes])
        clock.set(start)
        run.audits.extend(resolve_outcomes(world, now=clock.now()).audits)
        # The trigger's time is the episode's as-of time, so a late start carries its own time.
        started = trigger if start == trigger.at else trigger.model_copy(update={"at": start})
        try:
            episode = run.run_episode(
                world,
                started,
                f"Backtest trading episode: document {trigger.doc_id} as a new-document trigger, "
                "on the frozen snapshot and corpus.",
            )
        except RunBudgetExhausted as exc:
            run.stopped = f"the run's dollar cap was reached at {trigger.trigger_id}: {exc}"
            run.progress.triggers_not_run.extend(t.trigger_id for t in triggers[i:])
            return world
        run.progress.triggers_done.append(trigger.trigger_id)
        if episode.event_ticker is not None:
            event = world.exchange.get_event(episode.event_ticker)
            prices = market_prices(event)
            results = _results(data, episode.event_ticker)
            run.unresolved_rows.extend(
                f"{trigger.trigger_id} {ticker}: no final result in the snapshot"
                for ticker in sorted(prices)
                if ticker not in results
            )
            run.rows.extend(
                forecast_rows(
                    _pipeline_forecast(episode, episode.event_ticker),
                    run_id=run_id,
                    category=_category(config, event.series_ticker),
                    prices=prices,
                    results=results,
                    trigger_id=trigger.trigger_id,
                )
            )
    return world


def _late_starts(schedule: Schedule, world: World) -> dict[str, Any]:
    """How many trading episodes started later than their trigger, of how many, and the longest wait.

    An episode starts late when an earlier order's outcome was priced after its
    trigger's time (see ``_trading_pass``).
    """
    at = {t.trigger_id: t.at for t in schedule.triggers}
    waits = [
        (e.as_of - at[e.trigger.trigger_id]).total_seconds()
        for e in _episodes(world)
        if e.trigger.trigger_id in at
    ]
    late = [w for w in waits if w > 0]
    return {"episodes": len(late), "of": len(waits), "longest_seconds": max(late, default=0.0)}


def _category(config: Config, series_ticker: str) -> str:
    sc = config.follow.for_ticker(series_ticker)
    return sc.category if sc is not None else ""


class _NoModels:
    """Stands in where no system needs a model (a market-only run). Any call is a bug, so it raises."""

    def call(self, *args: Any, **kwargs: Any) -> ModelResult:
        raise RuntimeError(
            "no model is available in this run; no system that needs one may call it"
        )

    def usage(self, episode_id: str) -> Usage:
        return Usage()


class _NoKnowledge:
    """Stands in for the index in a market-only run, which retrieves nothing."""

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"no retrieval index in this run (asked for {name})")


# --------------------------------------------------------------------------- outputs


def _episodes(world: World | None) -> list[EpisodeRecord]:
    if world is None:
        return []
    found = world.store.list_episodes(limit=1_000_000)
    return sorted(found, key=lambda e: (e.as_of, e.episode_id))


def _in_run_order(world: World | None, ran: Sequence[str]) -> list[EpisodeRecord]:
    """One pass's stored episodes in the order the run made them (``_Run.ran``).

    The stored records are the ones to write, since the resolver has added to
    them; only the run knows their order. An episode the run did not record as
    run goes last, by as-of time; none is expected.
    """
    rank = {episode_id: i for i, episode_id in enumerate(ran)}
    return sorted(_episodes(world), key=lambda e: (rank.get(e.episode_id, len(rank)), e.as_of))


def _trades(
    data: BacktestData, world: World | None, episodes: list[EpisodeRecord]
) -> tuple[list[tuple[TradeRow, dict[str, str]]], list[OrderPricing]]:
    if world is None:
        return [], []
    settlements = {t: (r, at) for t, r, at in world.store.list_settlements()}
    by_id = {e.episode_id: e for e in episodes}
    trades: list[tuple[TradeRow, dict[str, str]]] = []
    pricing: list[OrderPricing] = []
    for order in world.store.list_orders():
        episode = by_id.get(order.episode_id)
        granularity = data.execution_book(order.market_ticker, order.placed_at).granularity
        pricing.append(
            OrderPricing(
                order_id=order.order_id,
                episode_id=order.episode_id,
                market_ticker=order.market_ticker,
                granularity=granularity,
                filled_contracts=order.filled_contracts,
                requested_contracts=order.requested_contracts,
            )
        )
        if episode is None or episode.routing is None or episode.event_ticker is None:
            continue
        settled = settlements.get(order.market_ticker)
        for fill in order.fills:
            row = TradeRow(
                episode_id=order.episode_id,
                event_ticker=episode.event_ticker,
                market_ticker=order.market_ticker,
                side=fill.side,
                contracts=fill.contracts,
                price=fill.price,
                fee=fill.fee,
                tier=episode.routing.tier,
                would_be_auto=episode.routing.would_be_auto,
                filled_at=fill.filled_at,
                result=settled[0] if settled else None,
                settled_at=settled[1] if settled else None,
            )
            trades.append(
                (
                    row,
                    {
                        "order_id": order.order_id,
                        "fill_tagged": str(episode.fill_tagged).lower(),
                        "pricing": granularity,
                    },
                )
            )
    return trades, pricing


def _write_forecasts(path: Path, rows: list[tuple[ForecastRow, RowNote]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FORECAST_COLUMNS)
        writer.writeheader()
        for row, note in rows:
            writer.writerow(
                {
                    "run_id": row.run_id,
                    "system": row.system,
                    "event_ticker": row.event_ticker,
                    "market_ticker": row.market_ticker,
                    "category": row.category,
                    "horizon_days": "" if row.horizon_days is None else row.horizon_days,
                    "trigger_id": row.trigger_id or "",
                    "as_of": row.as_of.isoformat(),
                    "probability": "" if row.probability is None else repr(row.probability),
                    "market_price": repr(row.market_price),
                    "outcome": int(row.outcome),
                    "episode_id": note.episode_id,
                    "note": note.note,
                }
            )


def read_forecasts(path: str | Path) -> list[tuple[ForecastRow, RowNote]]:
    """The per-forecast table back as rows and notes."""
    out = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            out.append(
                (
                    ForecastRow(
                        run_id=raw["run_id"],
                        system=raw["system"],
                        event_ticker=raw["event_ticker"],
                        market_ticker=raw["market_ticker"],
                        category=raw["category"],
                        horizon_days=int(raw["horizon_days"]) if raw["horizon_days"] else None,
                        trigger_id=raw["trigger_id"] or None,
                        as_of=datetime.fromisoformat(raw["as_of"]),
                        probability=float(raw["probability"]) if raw["probability"] else None,
                        market_price=float(raw["market_price"]),
                        outcome=raw["outcome"] == "1",
                    ),
                    RowNote(episode_id=raw["episode_id"], note=raw["note"]),
                )
            )
    return out


def _write_trades(path: Path, trades: list[tuple[TradeRow, dict[str, str]]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for t, extra in trades:
            writer.writerow(
                {
                    "episode_id": t.episode_id,
                    "order_id": extra["order_id"],
                    "event_ticker": t.event_ticker,
                    "market_ticker": t.market_ticker,
                    "side": t.side.value,
                    "contracts": str(t.contracts),
                    "price": str(t.price),
                    "fee": str(t.fee),
                    "tier": t.tier.value,
                    "would_be_auto": str(t.would_be_auto).lower(),
                    "fill_tagged": extra["fill_tagged"],
                    "filled_at": t.filled_at.isoformat(),
                    "result": t.result.value if t.result else "",
                    "settled_at": t.settled_at.isoformat() if t.settled_at else "",
                    "pricing": extra["pricing"],
                }
            )


def _choose_examples(episodes: list[EpisodeRecord], n: int) -> list[EpisodeRecord]:
    """Up to ``n`` episodes, deterministically: an executed trade, a no-trade exit, a review-tier case, then the rest."""
    ordered = sorted(episodes, key=lambda e: (e.as_of, e.episode_id))
    chosen: list[EpisodeRecord] = []

    def pick(test: Any) -> None:
        for e in ordered:
            if len(chosen) < n and e not in chosen and test(e):
                chosen.append(e)
                return

    pick(lambda e: e.exit is Exit.EXECUTED and any(o.filled_contracts for o in e.orders))
    pick(lambda e: e.exit in NO_TRADE_EXITS and e.forecast is not None)
    pick(lambda e: e.exit in NO_TRADE_EXITS)
    pick(lambda e: e.routing is not None and e.routing.tier.value == "approve")
    for e in ordered:
        if len(chosen) >= n:
            break
        if e not in chosen:
            chosen.append(e)
    return chosen[:n]


def replay_check(directory: Path) -> str:
    """Replay a saved trace offline and say how it went: ``replays and matches``, or what differed."""
    try:
        from pmfa.examples import check, replay

        result = replay(directory)
        problems = check(result)
    except Exception as exc:
        return f"does not replay: {type(exc).__name__}: {str(exc)[:300]}"
    if not problems:
        return "replays and matches"
    return "replays with differences: " + "; ".join(p[:200] for p in problems)


def _save_traces(
    run: _Run, worlds: list[World | None], episodes: list[EpisodeRecord], n: int
) -> dict[str, str]:
    from pmfa.world.recording import save_example

    folder = run.run_dir / TRACES_DIR
    shutil.rmtree(folder, ignore_errors=True)
    if n <= 0 or run.tap is None:
        return {}
    recorded = [e for e in episodes if e.episode_id in run.recordings and e.episode_id in run.exits]
    saved: dict[str, str] = {}
    for i, episode in enumerate(_choose_examples(recorded, n), 1):
        recording, trigger, description = run.recordings[episode.episode_id]
        card = None
        if episode.proposal is not None:
            for world in worlds:
                if world is None:
                    continue
                stored = world.store.get_proposal(episode.proposal.proposal_id)
                if stored is not None:
                    card = stored[2]
                    break
        exit_name = episode.exit.value if episode.exit else episode.status.value
        name = f"{i:02d}-{exit_name}-{episode.episode_id}"
        # The episode id does not depend on the run mode and the recorded world
        # carries the backtest's mode, so the cassette replays under its own key.
        save_example(
            folder / name,
            recording.file(trigger, description=description),
            run.tap.cassette(episode.episode_id),
            run.exits[episode.episode_id],
            card,
        )
        saved[f"{TRACES_DIR}/{name}"] = replay_check(folder / name)
    return saved


# --------------------------------------------------------------------------- the runner


def run_backtest(
    config: Config,
    *,
    run_dir: str | Path,
    systems: Sequence[str] | None = None,
    score: bool = True,
    trade: bool = True,
    limit: int | None = None,
    models: Models | None = None,
    dry_run: bool = False,
    save_examples: int = 3,
    clock: Clock | None = None,
) -> RunManifest:
    """Run (or, with ``dry_run``, plan) a backtest into ``run_dir`` and return its manifest.

    ``systems`` defaults to ``config.evaluation.systems``. ``models`` replaces
    the live gateway (tests pass ``ScriptedModels``); without it the gateway is
    used when the API key is set, and otherwise only the market runs.
    ``clock`` stamps the manifest (the system clock by default); the
    simulation runs on its own simulated clocks.
    """
    stamp = clock or SystemClock()
    config = backtest_config(config)
    chosen = _ordered_systems(systems, config)
    cutoffs = assert_window_after_cutoffs(config, config.evaluation.window_start)
    del cutoffs  # recorded per slot in the manifest
    run_dir = Path(run_dir)

    # The directory is created only once the data opened: a refused run leaves nothing behind.
    data = BacktestData(config)
    events = data.events()
    schedule = build_schedule(events, data.corpus, config, window_end=data.window_end, limit=limit)
    chosen_models, source = _choose_models(models, config, dry_run=dry_run)
    statuses = _statuses(chosen, source, score=score, trade=trade, dry_run=dry_run)
    plan_statuses = statuses
    plans, assumptions = plan_systems(
        schedule,
        {e.event_ticker: e for e in events},
        data.calendar,
        config,
        systems=chosen,
        statuses=plan_statuses,
        score=score,
        trade=trade,
        cache_dir=config.paths.cache_dir() / "models",
    )
    git = git_details()
    manifest = RunManifest(
        run_id=run_dir.name,
        name=run_dir.name,
        status="dry_run" if dry_run else "running",
        created_at=stamp.now(),
        git_commit=git.commit,
        git_dirty=git.dirty,
        git_changes=git.changes,
        snapshot_sha256=file_sha256(snapshot_path(config)),
        snapshot_content_sha256=(
            data.snapshot_manifest.content_sha256 or data.snapshot.content_sha256()
        ),
        corpus_hashes_digest=hashes_digest(data.corpus_manifest.content_hashes),
        seed=config.evaluation.seed,
        window_start=config.evaluation.window_start,
        window_end=data.window_end,
        systems_requested=chosen,
        systems=plan_statuses if dry_run else statuses,
        models=slot_info(config),
        models_source=source,
        snapshot_manifest=data.snapshot_manifest.model_dump(mode="json"),
        corpus_manifest=data.corpus_manifest.model_dump(mode="json", exclude={"content_hashes"}),
        plan=Plan(
            scoring_points=len(schedule.points),
            triggers=len(schedule.triggers),
            left_out_points=schedule.left_out_points,
            left_out_triggers=schedule.left_out_triggers,
            skipped_points=schedule.skipped_points,
            skipped_events=schedule.skipped_events,
            releases_without_event=releases_without_event(
                events,
                data.calendar.events,
                config,
                window_start=schedule.window_start,
                window_end=data.window_end,
            ),
            limit=limit,
            score=score,
            trade=trade,
            systems=plans,
            usd_max=sum((p.usd_max for p in plans), Decimal("0")),
            usd_min=sum((p.usd_min for p in plans), Decimal("0")),
            assumptions=assumptions,
        ),
        leakage_controls=LEAKAGE_CONTROLS,
        cost_controls=[
            *COST_CONTROLS,
            f"This run's dollar cap: ${config.budget.max_usd_per_run}.",
        ],
        fill_assumptions=FILL_ASSUMPTIONS,
        config=config.model_dump(mode="json"),
    )
    write_manifest(manifest, run_dir)
    if dry_run:
        data.close()
        return manifest

    running = [s for s in chosen if statuses[s] == "run"]
    tap = ModelTap(chosen_models, config) if chosen_models is not None else None
    for name in (WORK_DIR, TRACES_DIR):
        shutil.rmtree(run_dir / name, ignore_errors=True)
    (run_dir / WORK_DIR).mkdir(parents=True)
    run = _Run(data, config, run_dir, tap)
    index_built = any(s != MARKET for s in running)
    if index_built:
        manifest = manifest.model_copy(update={"index": data.index_info()})

    scoring_world: World | None = None
    trading_world: World | None = None
    trading_planned = trade and PIPELINE in running and tap is not None
    try:
        if score:
            scoring_world = _scoring_pass(run, schedule, running, tap, run_dir.name)
        if trading_planned:
            if run.stopped:
                run.progress.triggers_not_run.extend(t.trigger_id for t in schedule.triggers)
            else:
                trading_world = _trading_pass(run, schedule, tap, run_dir.name)
        for world in (scoring_world, trading_world):
            if world is not None and PIPELINE in running:
                # Settle the rest once every order's outcome has passed, so the
                # account the last settlements are marked from holds them all.
                store = world.store
                outcomes = [o.outcome_at for o in store.list_orders() if o.outcome_at is not None]
                world.clock.set(max([world.clock.now(), data.window_end, *outcomes]))  # type: ignore[attr-defined]
                run.audits.extend(resolve_outcomes(world, now=world.clock.now()).audits)
    except LeakageDetected as exc:
        aborted = manifest.model_copy(
            update={
                "status": "aborted",
                "finished_at": stamp.now(),
                "progress": run.progress.model_copy(update={"stopped_reason": str(exc)}),
                "leakage_checks": _leakage_checks(run, index_built=index_built),
                "notes": [*manifest.notes, f"aborted: {exc}"],
            }
        )
        write_manifest(aborted, run_dir)
        data.close()
        raise

    # -- outputs
    # Run order, which the self-halt of each pass depends on; the other uses of the list do not.
    episodes = _in_run_order(scoring_world, run.ran) + _in_run_order(trading_world, run.ran)
    trades, pricing = _trades(data, trading_world, _episodes(trading_world))
    _write_forecasts(run_dir / FORECASTS_FILE, run.rows)
    _write_trades(run_dir / TRADES_FILE, trades)
    with (run_dir / EPISODES_FILE).open("w", encoding="utf-8") as fh:
        for e in episodes:
            fh.write(e.model_copy(update={"trace": []}).model_dump_json() + "\n")
    saved = _save_traces(run, [scoring_world, trading_world], episodes, save_examples)

    final_status = _final_statuses(
        statuses,
        running,
        run.progress,
        points=len(schedule.points) if score else 0,
        triggers=len(schedule.triggers) if trading_planned else 0,
        stopped=run.stopped,
    )
    metrics = compute_metrics(
        [r for r, _ in run.rows],
        [n.note for _, n in run.rows],
        [t for t, _ in trades],
        episodes,
        run.audits,
        pricing,
        systems=final_status,
        config=config,
    )
    metrics["skipped_markets"] = run.skipped_markets
    metrics["unresolved_rows"] = run.unresolved_rows
    metrics["scored_events"] = sorted(
        {r.event_ticker for r, _ in run.rows if r.horizon_days is not None}
    )
    metrics["quotes"] = _quote_summary(run.quotes)
    if trading_world is not None:
        metrics["late_starts"] = _late_starts(schedule, trading_world)
    (run_dir / METRICS_FILE).write_text(
        json.dumps(metrics, indent=2, default=str) + "\n", encoding="utf-8"
    )

    usage = RunUsage()
    if tap is not None:
        run_usage = getattr(chosen_models, "run_usage", None)
        spent = run_usage() if callable(run_usage) else tap.usage_total
        usage = RunUsage(
            calls=spent.calls,
            input_tokens=spent.input_tokens,
            output_tokens=spent.output_tokens,
            cost_usd=spent.cost_usd,
            cache_answers=tap.sources.get("cache", 0),
            live_answers=tap.sources.get("live", 0),
        )
    notes = list(manifest.notes)
    if source == "supplied":
        notes.append(
            "Models were supplied by the caller (for example scripted models in a test); "
            "no number from this run is model output unless the caller passed the live gateway."
        )
    final = manifest.model_copy(
        update={
            "status": "partial" if run.stopped else "complete",
            "finished_at": stamp.now(),
            "systems": final_status,
            "progress": run.progress.model_copy(update={"stopped_reason": run.stopped}),
            "leakage_checks": _leakage_checks(run, index_built=index_built),
            "usage": usage,
            "outputs": [
                "manifest.json",
                FORECASTS_FILE,
                TRADES_FILE,
                EPISODES_FILE,
                METRICS_FILE,
                *saved,
            ],
            "traces": saved,
            "notes": notes,
        }
    )
    write_manifest(final, run_dir)
    for world in (scoring_world, trading_world):
        if world is not None:
            close = getattr(world.knowledge, "close", None)
            if callable(close):
                close()
            world.store.close()  # type: ignore[attr-defined]
    shutil.rmtree(run_dir / WORK_DIR / "index-scoring", ignore_errors=True)
    shutil.rmtree(run_dir / WORK_DIR / "index-trading", ignore_errors=True)
    data.close()
    return final


__all__ = [
    "EPISODES_FILE",
    "FORECASTS_FILE",
    "TRACES_DIR",
    "TRADES_FILE",
    "read_forecasts",
    "run_backtest",
]
