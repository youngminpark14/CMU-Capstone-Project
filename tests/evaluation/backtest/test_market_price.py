"""The market's price as the backtest scores it: the mid, else the last trade kept inside the quote."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmfa.config import Config
from pmfa.evaluation.backtest import read_forecasts, run_backtest
from pmfa.evaluation.baselines import market_price, price_source
from pmfa.records.market import OHLC, Candle
from pmfa.world.snapshot.store import SnapshotStore, snapshot_path
from tests.evaluation.backtest.support import CPI, CPI_CLOSE, OPEN, cpi_event


def _market(bid: str | None, ask: str | None, last: str | None):  # type: ignore[no-untyped-def]
    m = cpi_event().markets[2]
    return m.model_copy(
        update={
            "yes_bid": None if bid is None else Decimal(bid),
            "yes_ask": None if ask is None else Decimal(ask),
            "last_price": None if last is None else Decimal(last),
        }
    )


@pytest.mark.parametrize(
    ("bid", "ask", "last", "price", "source"),
    [
        ("0.14", "0.16", "0.50", "0.15", "mid"),
        (None, "0.01", "0.07", "0.01", "last_trade_clamped"),  # old trade above a no-bid ask
        ("0", "0.05", "0.03", "0.03", "last_trade"),  # inside the one-sided quote
        ("0.97", "1", "0.40", "0.97", "last_trade_clamped"),  # below a no-ask bid
        ("0.97", None, None, None, "none"),
    ],
)
def test_market_price_rule(
    bid: str | None, ask: str | None, last: str | None, price: str | None, source: str
) -> None:
    m = _market(bid, ask, last)
    assert price_source(m) == source
    assert market_price(m) == (None if price is None else Decimal(price))


def _ohlc(value: str | None) -> OHLC | None:
    if value is None:
        return None
    v = Decimal(value)
    return OHLC(open=v, high=v, low=v, close=v)


def _candle(ticker: str, at: datetime, bid: str | None, ask: str | None, px: str | None) -> Candle:
    return Candle(
        ticker=ticker,
        end_ts=at,
        interval_minutes=60,
        yes_bid=_ohlc(bid),
        yes_ask=_ohlc(ask),
        price=_ohlc(px),
        volume=Decimal("0") if px is None else Decimal("10"),
        open_interest=Decimal("100"),
    )


def test_an_old_trade_outside_a_one_sided_quote_is_scored_at_the_quote(
    data_config: Config, tmp_path: Path
) -> None:
    """The 3.0 strike trades once at 0.30 on Aug 1, then shows only an ask of 0.05 with no bid."""
    from pmfa.evaluation.report import SYSTEM_LABELS, build_report

    ticker = f"{CPI}-T3.0"
    hour = timedelta(hours=1)
    candles = [_candle(ticker, OPEN + hour, "0.29", "0.31", "0.30")]
    at = OPEN + 2 * hour
    while at <= CPI_CLOSE:
        candles.append(_candle(ticker, at, None, "0.05", None))
        at += hour
    store = SnapshotStore(snapshot_path(data_config))
    store.put_candles(ticker, 60, candles, start=OPEN, end=CPI_CLOSE)
    store.close()

    run_dir = tmp_path / "run"
    run_backtest(data_config, run_dir=run_dir, systems=["market"])
    rows = [r for r, _ in read_forecasts(run_dir / "forecasts.csv") if r.market_ticker == ticker]
    assert len(rows) == 3
    assert {r.market_price for r in rows} == {0.05}  # the ask, not the 0.30 trade

    quotes = json.loads((run_dir / "metrics.json").read_text())["quotes"]
    assert quotes["from_last_trade"] == 3 and quotes["last_trade_clamped"] == 3
    # The standing quote was an hour old at most; the trade was weeks old.
    assert quotes["quote_age_hours"]["max"] <= 1.0
    assert quotes["last_trade_age_hours"]["median"] > 24 * 7

    text = build_report(run_dir).read_text()
    assert SYSTEM_LABELS["market"] in text
    assert "stale" not in text.lower().replace("stale_price", "")
    assert "clamped into the standing quote" in text
