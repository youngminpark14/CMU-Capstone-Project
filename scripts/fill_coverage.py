"""How often a simulated order meets contracts to fill against, measured on the frozen backtest data.

A backtest order passes the gate only when its side has enough resting
liquidity (``caps.liquidity_floor_dollars``, a candle-volume proxy), and it
fills only against the candle that follows its arrival
(``pmfa.world.snapshot.book``). One-minute candles are sparse, so many orders
that pass the gate meet no candle and go unfilled. This script counts, at every
news trigger of the trading pass, the open market sides that pass the liquidity
floor and how many of those had contracts to fill against. It reads the data
and writes nothing.

Two counts bracket how many could fill. Contracts at any price is the upper
bound: an order fills only up to its limit. Contracts at or below the ask the
order was sized on (the as-of ask) is the lower bound: the limit can sit above
that ask, up to the edge-preserving bound. The times are the triggers' own, so
a start delayed by an earlier order's outcome is not modelled.

Run it after ``pmfa data build``::

    uv run python scripts/fill_coverage.py

The numbers say what to expect from the fill-failure stop
(``oversight.fill_failure_window``) in a backtest; ``docs/evaluation.md``
quotes them under "Fill assumptions and their limits".
"""

from __future__ import annotations

import sys
from collections import Counter

from pmfa.config import Config, load_config
from pmfa.errors import NotFound
from pmfa.evaluation.schedule import build_schedule
from pmfa.records.market import Side
from pmfa.world.backtest import BacktestData, backtest_config
from pmfa.world.clocks import SimClock
from pmfa.world.snapshot.reader import SnapshotExchange


def measure(config: Config) -> dict[str, int]:
    """Count market sides at each trading trigger: open, liquid, and with contracts to fill against."""
    data = BacktestData(config)
    try:
        events = data.events()
        schedule = build_schedule(events, data.corpus, config, window_end=data.window_end)
        clock = SimClock(schedule.window_start)
        exchange = SnapshotExchange(data.snapshot, clock, config)
        floor = config.caps.liquidity_floor_dollars
        counts: Counter[str] = Counter(triggers=len(schedule.triggers))
        for trigger in schedule.triggers:
            clock.set(max(trigger.at, clock.now()))
            now = clock.now()
            fillable_here = False
            for listed in events:
                try:
                    event = exchange.get_event(listed.event_ticker)
                except NotFound:
                    continue  # not listed yet at this time
                for market in event.markets:
                    if market.close_time <= now:
                        continue
                    for side in Side:
                        counts["sides"] += 1
                        try:
                            resting = exchange.liquidity_dollars(market.ticker, side)
                        except NotFound:
                            continue  # no candle history for this market yet
                        if resting < floor:
                            continue
                        counts["liquid"] += 1
                        found = data.execution_book(market.ticker, now)
                        counts[f"liquid_{found.granularity}"] += 1
                        offered = [lvl.price for lvl in found.book.asks(side) if lvl.contracts > 0]
                        if not offered:
                            continue
                        counts["liquid_fillable"] += 1
                        fillable_here = True
                        sized_on = exchange.get_order_book(market.ticker).best_ask(side)
                        if sized_on is not None and min(offered) <= sized_on.price:
                            counts["liquid_fillable_at_as_of_ask"] += 1
            counts["triggers_with_a_fillable_side"] += fillable_here
        return dict(counts)
    finally:
        data.close()


def main() -> int:
    config = backtest_config(load_config())
    c = Counter(measure(config))
    liquid, fillable, at_ask = c["liquid"], c["liquid_fillable"], c["liquid_fillable_at_as_of_ask"]

    def share(n: int) -> str:
        return f"{n / liquid:.0%}" if liquid else "n/a"

    lines = [
        f"news triggers in the trading pass: {c['triggers']}",
        f"open market sides at those triggers: {c['sides']}",
        f"  pass the liquidity floor of ${config.caps.liquidity_floor_dollars}: {liquid}",
        f"    had contracts at any price in the fill candle: {fillable} ({share(fillable)})",
        f"      of which at or below the as-of ask: {at_ask} ({share(at_ask)})",
        f"    had none: {liquid - fillable} ({share(liquid - fillable)})",
        f"    fill candle: one-minute {c['liquid_minute']}, hourly fallback {c['liquid_hour']}, "
        f"none {c['liquid_none']}",
        f"triggers with at least one liquid, fillable side: {c['triggers_with_a_fillable_side']}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
