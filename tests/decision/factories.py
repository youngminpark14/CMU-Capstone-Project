"""Small builders for decision-core tests: one Fed-decision event, books, and contexts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import settings

from pmfa.config import Config, SeriesConfig
from pmfa.decision import GateContext, RoutingContext, SizingInputs
from pmfa.records.base import Stage
from pmfa.records.episode import Calibration, RiskState
from pmfa.records.evidence import Origin
from pmfa.records.market import (
    BookLevel,
    Market,
    MarketStatus,
    OrderBook,
    Position,
    PriceRange,
    Series,
    Side,
)

NOW = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
SERIES_TICKER = "KXFEDDECISION"
EVENT = "KXFEDDECISION-26DEC"
HOLD = f"{EVENT}-H0"
CUT = f"{EVENT}-C25"
CONFIG = Config()


def series(fee_type: str = "quadratic_with_maker_fees", multiplier: str = "1") -> Series:
    return Series(
        ticker=SERIES_TICKER,
        title="Fed decision",
        category="fed_decision",
        fee_type=fee_type,
        fee_multiplier=Decimal(multiplier),
    )


def series_config(shrinkage: float = 0.0) -> SeriesConfig:
    base = CONFIG.follow.for_ticker(SERIES_TICKER)
    assert base is not None
    return base.model_copy(update={"shrinkage": shrinkage})


def market(ticker: str = HOLD, price_ranges: list[PriceRange] | None = None) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=EVENT,
        title=ticker,
        status=MarketStatus.OPEN,
        open_time=datetime(2026, 10, 1, tzinfo=UTC),
        close_time=datetime(2026, 12, 9, 19, tzinfo=UTC),
        price_ranges=price_ranges or [],
    )


def book(
    ticker: str = HOLD,
    yes_bids: list[tuple[str, str]] = (("0.48", "100"),),
    no_bids: list[tuple[str, str]] = (("0.50", "100"),),
) -> OrderBook:
    """Bids best first. YES asks are ``1 - no bid``; NO asks are ``1 - yes bid``."""
    return OrderBook(
        ticker=ticker,
        as_of=NOW,
        yes_bids=[BookLevel(price=p, contracts=c) for p, c in yes_bids],
        no_bids=[BookLevel(price=p, contracts=c) for p, c in no_bids],
    )


def position(
    ticker: str = HOLD, side: Side = Side.YES, contracts: int = 10, avg: str = "0.50"
) -> Position:
    return Position(
        market_ticker=ticker, event_ticker=EVENT, side=side, contracts=contracts, avg_price=avg
    )


def inputs(
    forecast: dict[str, float] | None = None,
    books: dict[str, OrderBook] | None = None,
    markets: list[Market] | None = None,
    liquidity: dict[str, dict[Side, Decimal]] | None = None,
    positions: list[Position] | None = None,
    bankroll: str = "1000",
    confidence: float = 1.0,
    shrinkage: float = 0.0,
    fee_series: Series | None = None,
) -> SizingInputs:
    books = books if books is not None else {HOLD: book()}
    markets = markets if markets is not None else [market(t) for t in books]
    if liquidity is None:
        liquidity = {t: {Side.YES: Decimal("1000"), Side.NO: Decimal("1000")} for t in books}
    return SizingInputs(
        episode_id="ep-1",
        event_ticker=EVENT,
        forecast_yes=forecast if forecast is not None else {HOLD: 0.70},
        markets=markets,
        books=books,
        liquidity=liquidity,
        positions=positions or [],
        bankroll=Decimal(bankroll),
        confidence=confidence,
        series=fee_series or series(),
        series_config=series_config(shrinkage),
        created_at=NOW,
    )


def risk(
    bankroll: str = "1000",
    day_pnl: str = "0",
    drawdown: str = "0",
    loss_stop: bool = False,
    drawdown_halt: bool = False,
) -> RiskState:
    b = Decimal(bankroll)
    return RiskState(
        bankroll=b,
        day_start_equity=b,
        peak_equity=b,
        day_pnl=Decimal(day_pnl),
        drawdown_fraction=Decimal(drawdown),
        loss_stop_tripped=loss_stop,
        drawdown_tripped=drawdown_halt,
    )


PRIMARY = Origin(
    origin_id="o-fed",
    source="federalreserve.gov",
    url="https://www.federalreserve.gov/x",
    published_at=datetime(2026, 11, 20, tzinfo=UTC),
    is_primary=True,
)


def secondary(i: int) -> Origin:
    return Origin(
        origin_id=f"o-wire-{i}",
        source=f"wire{i}.example",
        url=f"https://wire{i}.example/a",
        published_at=datetime(2026, 11, 20, tzinfo=UTC),
        is_primary=False,
    )


def gate_context(
    sized: SizingInputs,
    market_ticker: str = HOLD,
    **overrides: object,
) -> GateContext:
    """A context that agrees with ``sized``: same book, forecast, holdings, and bankroll."""
    fields: dict[str, object] = {
        "config": CONFIG,
        "now": NOW,
        "halted": False,
        "stage": Stage.SHADOW,
        "simulated": True,
        "series": sized.series,
        "series_config": sized.series_config,
        "market": next(m for m in sized.markets if m.ticker == market_ticker),
        "book": sized.books[market_ticker],
        "forecast_yes": sized.forecast_yes[market_ticker],
        "origins": [PRIMARY],
        "liquidity_dollars": Decimal("1000"),
        "positions": sized.positions,
        "risk": risk(bankroll=str(sized.bankroll)),
    }
    fields.update(overrides)
    return GateContext(**fields)


def routing_context(**overrides: object) -> RoutingContext:
    """A context in which every auto condition holds, at the autonomous stage."""
    fields: dict[str, object] = {
        "config": CONFIG,
        "stage": Stage.AUTONOMOUS,
        "halted": False,
        "risk": risk(),
        "liquidity_dollars": Decimal("1000"),
        "calibration": Calibration(category="fed_decision", n_resolved=40, calibration_error=0.05),
        "price_move": Decimal("0.01"),
    }
    fields.update(overrides)
    return RoutingContext(**fields)


PROPERTY = settings(derandomize=True, database=None, deadline=None, max_examples=200)
"""Hypothesis settings for this package: deterministic, and nothing written to disk."""
