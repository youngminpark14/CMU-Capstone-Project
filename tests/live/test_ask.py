"""``pmfa ask``: choosing the event and the market, the focused world, the sources, one episode.

Every world here is the constructed Bitcoin hour of ``tests.live.ask_support``;
nothing touches the network. The tests assert what was decided: the event and
market chosen, the exit, the forecast, the proposal, the orders, and what the
folder holds.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pmfa.cli.state import status_report
from pmfa.config import Config
from pmfa.errors import ConfigError, NotFound
from pmfa.evaluation.resolver import resolve_outcomes
from pmfa.live import ask as ask_module
from pmfa.live import session as session_module
from pmfa.live.ask import (
    AskError,
    asked_config,
    asked_trigger,
    check_stage,
    choose_event,
    choose_market,
    marked_text,
    run_ask,
)
from pmfa.records.base import RunMode, Stage
from pmfa.records.decision import ProposalStatus, ReviewDecision
from pmfa.records.episode import EpisodeStatus, Exit
from pmfa.records.evidence import Document, TriggerKind
from pmfa.records.market import Event, Market, MarketStatus, Side
from pmfa.records.roles import RoleTask
from pmfa.state.store import SqliteStore
from pmfa.world.evidence.common import content_hash
from pmfa.world.evidence.corpus import CorpusStore
from pmfa.world.evidence.http import FetchError
from pmfa.world.evidence.live import LiveEvidence
from pmfa.world.evidence.spot import COINBASE_SOURCE, SpotError, snapshot_document
from pmfa.world.focus import FocusedExchange, focus_world
from pmfa.world.live import live_corpus_dir, live_paths

from .ask_support import (
    AS_OF,
    ATM,
    COINBASE,
    EVENING,
    HOUR,
    Built,
    FakeFetcher,
    ask_config,
    ask_world,
    coinbase_pages,
    events,
    market,
    world_file,
)

# --------------------------------------------------------------------------- helpers


def _market(ticker: str, bid: str | None, ask: str | None, close: datetime) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        title="Bitcoin price?",
        status=MarketStatus.OPEN,
        open_time=close - timedelta(hours=1),
        close_time=close,
        strike_type="greater",
        floor_strike=Decimal("1"),
        yes_bid=Decimal(bid) if bid is not None else None,
        yes_ask=Decimal(ask) if ask is not None else None,
    )


def _event(ticker: str, close: datetime, quotes: list[tuple[str, str, str]]) -> Event:
    return Event(
        event_ticker=ticker,
        series_ticker="KXBTCD",
        title=ticker,
        markets=[_market(f"{ticker}-{s}", b, a, close) for s, b, a in quotes],
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """The loaded configuration of these tests: KXBTCD is switched off in it, as by default."""
    return Config.model_validate(ask_config(tmp_path / "data"))


@pytest.fixture
def patched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Built:
    """``build_world`` gives the Bitcoin world, and the snapshot reads the constructed Coinbase."""
    built = Built()

    def fake_build_world(mode, config, *, review=False, models=None):  # type: ignore[no-untyped-def]
        return ask_world(config, review=review, built=built, at=built.at)

    monkeypatch.setattr(session_module, "build_world", fake_build_world)
    monkeypatch.setattr(
        ask_module, "snapshot_fetcher", lambda config: FakeFetcher(coinbase_pages())
    )
    return built


def _ask(config: Config, decision: str = "approve", **kwargs: Any) -> tuple[Any, list[str]]:
    said: list[str] = []
    result = run_ask(
        config,
        "KXBTCD",
        decide=lambda card: (ReviewDecision(decision), None),
        say=said.append,
        **kwargs,
    )
    return result, said


# --------------------------------------------------------------------------- choosing


def test_the_event_closing_soonest_inside_the_window_is_chosen() -> None:
    now = datetime(2026, 11, 28, 5, 22, tzinfo=UTC)
    chosen = choose_event(events(), now, min_minutes=10, window_minutes=60)
    assert chosen.event_ticker == HOUR


def test_an_event_about_to_close_is_passed_over_for_the_next_in_the_window() -> None:
    now = datetime(2026, 11, 28, 5, 55, tzinfo=UTC)
    closing = _event("KXBTCD-A", now + timedelta(minutes=5), [("T1", "0.4", "0.5")])
    next_one = _event("KXBTCD-B", now + timedelta(minutes=40), [("T1", "0.4", "0.5")])
    chosen = choose_event([closing, next_one], now, min_minutes=10, window_minutes=60)
    assert chosen.event_ticker == "KXBTCD-B"


def test_no_event_in_the_window_says_when_the_current_one_closes_and_names_market() -> None:
    now = datetime(2026, 11, 28, 5, 55, tzinfo=UTC)
    with pytest.raises(AskError) as caught:
        choose_event(events(), now, min_minutes=10, window_minutes=60)
    message = str(caught.value)
    assert "closes at 06:00 UTC, in 5 minute(s)" in message
    assert "next one opens when it closes" in message and "--market" in message


def test_just_after_the_hour_the_message_says_the_next_event_is_not_listed_yet() -> None:
    now = datetime(2026, 11, 28, 6, 0, 5, tzinfo=UTC)
    evening = [e for e in events() if e.event_ticker == EVENING]
    with pytest.raises(AskError) as caught:
        choose_event(evening, now, min_minutes=10, window_minutes=60)
    assert "lists the next one shortly after the hour: try again in a minute" in str(caught.value)


def test_the_market_nearest_even_odds_is_chosen_and_a_tie_goes_to_the_lowest_ticker() -> None:
    hour = next(e for e in events() if e.event_ticker == HOUR)
    assert choose_market(hour).ticker == ATM
    close = datetime(2026, 11, 28, 6, tzinfo=UTC)
    tied = _event("KXBTCD-X", close, [("T2", "0.45", "0.47"), ("T1", "0.53", "0.55")])
    assert choose_market(tied).ticker == "KXBTCD-X-T1"


def test_a_market_quoted_on_one_side_only_is_never_chosen() -> None:
    close = datetime(2026, 11, 28, 6, tzinfo=UTC)
    one_sided = _event("KXBTCD-X", close, [("T1", "0", "0.03"), ("T2", "0.99", "1")])
    with pytest.raises(AskError, match="both a bid and an ask"):
        choose_market(one_sided)


def test_an_unknown_series_is_an_error_naming_the_configured_ones() -> None:
    with pytest.raises(ConfigError) as caught:
        asked_config(Config(), "KXNOPE")
    assert "KXFEDDECISION" in str(caught.value) and "KXBTCD" in str(caught.value)


def test_asking_switches_only_the_asked_series_on() -> None:
    before = Config()
    after = asked_config(before, "KXBTCD")
    on = {s.ticker for s in after.follow.enabled_series()}
    assert on == {s.ticker for s in before.follow.enabled_series()} | {"KXBTCD"}
    assert after.model_dump(exclude={"follow"}) == before.model_dump(exclude={"follow"})


def test_an_autonomous_stage_is_refused_so_every_proposal_waits(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.sqlite")
    check_stage(store)  # the default backtest stage
    store.set_stage(Stage.AUTONOMOUS, "test", AS_OF)
    with pytest.raises(AskError, match="autonomous"):
        check_stage(store)
    store.close()


def test_the_trigger_is_a_schedule_trigger_naming_the_event() -> None:
    trigger = asked_trigger(HOUR, AS_OF)
    assert trigger.kind is TriggerKind.SCHEDULE and trigger.event_ticker == HOUR
    assert trigger.trigger_id == f"schedule:{HOUR}:asked:20261128T052200Z"
    assert trigger.at == AS_OF


# --------------------------------------------------------------------------- the default config


def test_with_the_default_configuration_the_bitcoin_series_is_followed_by_nothing(
    tmp_path: Path,
) -> None:
    config = Config()
    btc = config.follow.for_ticker("KXBTCD")
    assert btc is not None and not btc.enabled
    assert "KXBTCD" not in {s.ticker for s in config.follow.enabled_series()}
    evidence = LiveEvidence(config, FakeFetcher({}), CorpusStore(tmp_path / "corpus"))
    assert evidence.feeds() == []
    asked = LiveEvidence(
        asked_config(config, "KXBTCD"), FakeFetcher({}), CorpusStore(tmp_path / "corpus")
    )
    assert asked.feeds() == btc.feeds


# --------------------------------------------------------------------------- the focused world


class Recording:
    """An exchange that records every call and answers each with a fixed value."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.hour = next(e for e in events() if e.event_ticker == HOUR)
        self.evening = next(e for e in events() if e.event_ticker == EVENING)

    def get_event(self, ticker: str) -> Event:
        self.calls.append("get_event")
        return self.hour if ticker == HOUR else self.evening

    def list_events(self, series: str, *, include_closed: bool = False) -> list[Event]:
        self.calls.append("list_events")
        return [self.hour, self.evening]

    def get_series(self, series: str) -> Any:
        self.calls.append("get_series")
        return "series"

    def get_market(self, ticker: str) -> Any:
        self.calls.append("get_market")
        return "market"

    def get_order_book(self, ticker: str) -> Any:
        self.calls.append("get_order_book")
        return "book"

    def get_candles(self, ticker: str, **kwargs: Any) -> Any:
        self.calls.append("get_candles")
        return ["candle"]

    def liquidity_dollars(self, ticker: str, side: Side) -> Any:
        self.calls.append("liquidity_dollars")
        return Decimal("7")


def test_the_focused_event_has_one_market_and_every_other_call_passes_through() -> None:
    inner = Recording()
    focused = FocusedExchange(inner, HOUR, ATM)
    assert [m.ticker for m in focused.get_event(HOUR).markets] == [ATM]
    listed = {e.event_ticker: e for e in focused.list_events("KXBTCD")}
    assert [m.ticker for m in listed[HOUR].markets] == [ATM]
    assert len(listed[EVENING].markets) == 2  # another event is left as it is
    assert focused.get_series("KXBTCD") == "series"
    assert focused.get_market(ATM) == "market"
    assert focused.get_order_book(ATM) == "book"
    start, end = AS_OF - timedelta(hours=1), AS_OF
    assert focused.get_candles(ATM, start=start, end=end, interval_minutes=1) == ["candle"]
    assert focused.liquidity_dollars(ATM, Side.YES) == Decimal("7")
    assert inner.calls == [
        "get_event",
        "list_events",
        "get_series",
        "get_market",
        "get_order_book",
        "get_candles",
        "liquidity_dollars",
    ]


def test_a_focused_market_the_event_no_longer_lists_is_not_found() -> None:
    with pytest.raises(NotFound):
        FocusedExchange(Recording(), HOUR, f"{HOUR}-T1.00").get_event(HOUR)


def test_the_focused_world_reads_the_market_alone_and_its_paper_executor_reads_through_it(
    config: Config,
) -> None:
    world = ask_world(asked_config(config, "KXBTCD"))
    focused = focus_world(world, HOUR, ATM)
    assert [m.ticker for m in focused.exchange.get_event(HOUR).markets] == [ATM]
    assert focused.executor.exchange is focused.exchange  # type: ignore[attr-defined]
    assert focused.store is world.store and focused.clock is world.clock
    session_module.close_world(world)


# --------------------------------------------------------------------------- the price snapshot


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def test_the_snapshot_is_one_dated_document_in_plain_words_with_exact_prices() -> None:
    fetcher = FakeFetcher(coinbase_pages())
    doc = snapshot_document(fetcher, "BTC-USD", base_url=COINBASE, clock=Clock(AS_OF))
    assert doc.doc_id == "spot:BTC-USD:20261128T052200Z"
    assert doc.source == COINBASE_SOURCE and doc.published_at == AS_OF
    assert doc.url == f"{COINBASE}/products/BTC-USD/ticker"
    assert doc.title == "Bitcoin (BTC-USD) price on Coinbase at 05:22 UTC, November 28, 2026"
    text = doc.text
    assert "$59,812.34 U.S. dollars" in text  # the ticker's price string, exactly
    assert "Fifteen minutes earlier" in text and "About 24 hours earlier" in text
    assert re.search(r"Over the last 60 minutes the Bitcoin price ranged", text)
    assert "standard deviation of one-minute BTC price returns" in text
    assert "five-minute steps" in text and "Real-Time Index" in text
    assert doc.content_hash == content_hash(text)
    assert len(fetcher.calls) == 3
    # The candles dated after the clock (close $99,999, low and high $5 either side) are left out.
    assert "$99,99" not in text and "$100,00" not in text


def test_the_one_minute_volatility_skips_gaps_and_the_minute_still_forming() -> None:
    # Constructed: closes alternate by $1 a minute; 05:00-05:04 are missing (no trades), and
    # the price is $600 higher after the gap; the minute still forming (05:22) closes far off.
    rows = []
    for i in range(70):
        start = AS_OF - timedelta(minutes=69 - i)
        if (
            datetime(2026, 11, 28, 5, 0, tzinfo=UTC)
            <= start
            < datetime(2026, 11, 28, 5, 5, tzinfo=UTC)
        ):
            continue
        close = 60000 + (i % 2) + (600 if start >= datetime(2026, 11, 28, 5, tzinfo=UTC) else 0)
        if start == AS_OF:
            close = 61000
        rows.append([int(start.timestamp()), close - 5, close + 5, close, close, 1.0])
    pages = coinbase_pages()
    pages[f"{COINBASE}/products/BTC-USD/candles?granularity=60"] = json.dumps(rows[::-1])
    doc = snapshot_document(FakeFetcher(pages), "BTC-USD", base_url=COINBASE, clock=Clock(AS_OF))
    # Every one-minute return is about 0.0017%; the gap or the forming minute would make it 0.1%.
    assert "returns over the last 60 minutes was 0.002%" in doc.text


def test_a_snapshot_that_cannot_be_read_raises_a_project_error() -> None:
    with pytest.raises(FetchError):
        snapshot_document(FakeFetcher({}), "BTC-USD", base_url=COINBASE, clock=Clock(AS_OF))
    pages = coinbase_pages()
    pages[f"{COINBASE}/products/BTC-USD/candles?granularity=60"] = "[]"
    with pytest.raises(SpotError):
        snapshot_document(FakeFetcher(pages), "BTC-USD", base_url=COINBASE, clock=Clock(AS_OF))


def test_marked_text_puts_the_used_passages_words_in_bold() -> None:
    config = Config.model_validate({"retrieval": {"passage_words": 4, "passage_overlap_words": 1}})
    doc = Document(
        doc_id="d",
        source="example.org",
        kind="news",
        url="https://example.org/d",
        title="T",
        published_at=AS_OF,
        text="one two three four\n\nfive six seven",
    )
    # Passages: words 0-3, then 3-6 (one word of overlap).
    assert marked_text(doc, (1,), config) == "one two three **four**\n\n**five six seven**"
    assert marked_text(doc, (), config) == "one two three four\n\nfive six seven"


# --------------------------------------------------------------------------- one asked run


def test_an_approved_ask_ends_executed_with_a_paper_fill_and_saves_its_folder(
    config: Config, patched: Built
) -> None:
    result, said = _ask(config, "approve")
    record = result.record
    assert result.exit_code == 0
    assert record.exit is Exit.EXECUTED and record.event_ticker == HOUR
    assert set(record.forecast.market_probabilities) == {ATM}
    assert record.forecast.market_probabilities[ATM] > 0.5
    proposal = record.proposal
    assert proposal.side is Side.YES and proposal.market_ticker == ATM
    assert proposal.contracts > 0 and proposal.limit_price == Decimal("0.49")
    assert record.review is not None and record.review.decision is ReviewDecision.APPROVE
    [order] = record.orders
    assert order.filled_contracts == proposal.contracts and all(f.simulated for f in order.fills)
    assert record.trigger.kind is TriggerKind.SCHEDULE

    # The match step made no model call; four calls in all, on the fast path.
    models = patched.models[0]
    assert [c.task for c in models.calls] == [
        RoleTask.PLAN_QUERIES,
        RoleTask.SELECT_PASSAGES,
        RoleTask.GENERATE,
        RoleTask.SCORE,
    ]
    assert record.forecast.fast_path

    # The snapshot was kept and counts as a primary origin.
    primary = [o for o in record.origins if o.is_primary]
    assert [o.source for o in primary] == [COINBASE_SOURCE]

    # The folder.
    folder = result.folder
    assert folder.name == f"20261128T052200Z-{ATM}"
    answer = (folder / "answer.md").read_text(encoding="utf-8")
    assert "The agent's answer: YES" in answer and "Your decision: approve" in answer
    assert "fill" in answer
    episode = json.loads((folder / "episode.json").read_text(encoding="utf-8"))
    assert episode["episode_id"] == record.episode_id and episode["exit"] == "executed"
    sources = sorted((folder / "sources").iterdir())
    assert [p.name[:2] for p in sources] == ["01", "02"]
    snapshot = next(p for p in sources if "coinbase-com" in p.name)
    body = snapshot.read_text(encoding="utf-8")
    assert "- Origin: primary" in body and "**" in body and "$59,812.34" in body
    assert any(str(folder) in line for line in said)


def test_no_role_input_holds_the_markets_price(config: Config, patched: Built) -> None:
    _ask(config, "reject")
    quote = market(ATM)
    prices = {str(quote.yes_bid), str(quote.yes_ask), "0.47", "0.49", "0.48", "0.51", "0.53"}
    for call in patched.models[0].calls:
        dumped = call.payload.model_dump(mode="json")
        text = json.dumps(dumped)
        for key in ("yes_bid", "yes_ask", "last_price", "market_price", "mid"):
            assert f'"{key}"' not in text, (call.task, key)
        # 0.47, $0.47 and 47 cents (47c, 47¢) are all the same price written three ways.
        numbers = set(re.findall(r"(?<![\d.,])\$?(0\.\d+)(?![\d%])", text))
        assert not numbers & prices, (call.task, numbers & prices)
        cents = {str(int(Decimal(p) * 100)) for p in prices}
        written = set(re.findall(r"(?<![\d.,$])(\d{2})\s?(?:¢|c\b|cents?\b)", text))
        assert not written & cents, (call.task, written & cents)


def test_a_rejected_ask_ends_rejected_by_human_with_no_order(
    config: Config, patched: Built
) -> None:
    result, _ = _ask(config, "reject")
    record = result.record
    assert record.exit is Exit.REJECTED_BY_HUMAN and record.orders == []
    with SqliteStore(live_paths(config, record.mode)[0]) as store:
        assert store.list_orders() == []
        assert store.get_proposal(record.proposal.proposal_id)[1] is ProposalStatus.REJECTED
    assert "Your decision: reject" in (result.folder / "answer.md").read_text(encoding="utf-8")


def test_a_snapshot_that_cannot_be_read_is_a_note_and_the_episode_ends_truthfully(
    config: Config, patched: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ask_module, "snapshot_fetcher", lambda config: FakeFetcher({}))
    result, said = _ask(config, "approve")
    record = result.record
    # One feed item alone is one secondary origin: the origin rule is not met.
    assert record.exit is Exit.INSUFFICIENT_ORIGINS and record.forecast is None
    assert record.orders == [] and record.proposal is None
    assert any(line.startswith("note: price snapshot of BTC-USD not read") for line in said)
    answer = (result.folder / "answer.md").read_text(encoding="utf-8")
    assert "The agent has no answer" in answer and "insufficient_origins" in answer
    assert len(list((result.folder / "sources").iterdir())) == 1  # the feed item it kept


def test_nothing_published_after_the_trigger_is_read(config: Config, patched: Built) -> None:
    result, _ = _ask(config, "reject")
    record = result.record
    assert all(o.published_at <= record.as_of for o in record.origins)
    for call in patched.models[0].calls:
        assert "Published after the as-of time" not in call.payload.model_dump_json()


def test_market_names_the_market_whatever_its_price(config: Config, patched: Built) -> None:
    other = f"{HOUR}-T59699.99"
    result, _ = _ask(config, "reject", market_ticker=other)
    assert result.record.event_ticker == HOUR
    assert set(result.record.forecast.market_probabilities) == {other}
    assert result.folder.name.endswith(other)


def test_market_on_a_later_event_warns_that_the_full_tree_runs(
    config: Config, patched: Built
) -> None:
    _, said = _ask(config, "reject", market_ticker=f"{EVENING}-T59499.99")
    assert any("full scenario tree" in line for line in said)


def test_no_event_in_the_window_spends_nothing_and_exits_1(config: Config, patched: Built) -> None:
    patched.at = datetime(2026, 11, 28, 5, 55, tzinfo=UTC)
    result, said = _ask(config, "approve")
    assert result.exit_code == 1 and result.record is None and result.folder is None
    assert patched.models[0].calls == [] and patched.feeds[0].calls == []
    assert any("closes at 06:00 UTC" in line for line in said)


def test_an_asked_forecast_resolves_and_its_paper_position_settles_under_the_default_config(
    config: Config, patched: Built, tmp_path: Path
) -> None:
    result, _ = _ask(config, "approve")
    record = result.record
    # Later, with the default configuration (KXBTCD switched off), after the market settled.
    assert not config.follow.for_ticker("KXBTCD").enabled
    world = ask_world(config, at=AS_OF + timedelta(hours=2), file=world_file(settled=Side.YES))
    report = resolve_outcomes(world)
    assert report.markets_resolved == [ATM] and report.markets_settled == [ATM]
    [entry] = [e for e in world.store.ledger() if e.episode_id == record.episode_id]
    assert entry.outcome is True and entry.category == "crypto"
    [audit] = report.audits
    assert audit.episode_id == record.episode_id
    assert audit.audit.realized_outcome_id == "gt_59799.99"
    assert world.executor.positions() == []  # settled and paid out
    report = status_report(world.store, config, RunMode.SHADOW)
    assert any(line.startswith("  crypto: 1 forecast(s)") for line in report)
    assert any("over 1 settled of 1 trade(s)" in line for line in report)
    session_module.close_world(world)


def test_a_market_kalshi_does_not_list_is_refused_before_anything_is_read(
    config: Config, patched: Built
) -> None:
    result, said = _ask(config, "approve", market_ticker="KXFEDDECISION-26DEC-H0")
    assert result.exit_code == 1 and result.record is None
    assert any(line.startswith("not run: Kalshi lists no market") for line in said)
    assert patched.models[0].calls == [] and patched.feeds[0].calls == []


def test_the_episode_paused_for_review_before_the_decision(config: Config, patched: Built) -> None:
    seen: list[Any] = []

    def decide(card: Any) -> Any:
        [episode] = patched.worlds[0].store.list_episodes()
        seen.append((card.market_ticker, episode.status))
        return ReviewDecision.REJECT, None

    said: list[str] = []
    result = run_ask(config, "KXBTCD", decide=decide, say=said.append)
    assert seen == [(ATM, EpisodeStatus.PENDING_REVIEW)]
    assert result.record.status is EpisodeStatus.DONE
    # The run never points at a command that could not decide the proposal later.
    assert not any("pmfa review approve" in line for line in said)


def test_a_failed_snapshot_read_leaves_no_earlier_snapshot_to_forecast_on(
    config: Config, patched: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ask(config, "reject")  # an earlier run stored a snapshot
    patched.at = AS_OF + timedelta(minutes=5)
    monkeypatch.setattr(ask_module, "snapshot_fetcher", lambda config: FakeFetcher({}))
    result, said = _ask(config, "approve")
    record = result.record
    assert not [o for o in record.origins if o.source == COINBASE_SOURCE]
    assert record.exit is Exit.INSUFFICIENT_ORIGINS and record.orders == []
    assert any(line.startswith(f"Episode {record.episode_id}: pmfa trace") for line in said)


def test_a_halted_shadow_mode_is_refused_before_anything_is_read(
    config: Config, patched: Built
) -> None:
    state_db = live_paths(config, RunMode.SHADOW)[0]
    state_db.parent.mkdir(parents=True)
    with SqliteStore(state_db) as store:
        store.set_halt("self-halt: source withdrawn", AS_OF)
    result, said = _ask(config, "approve")
    assert result.exit_code == 1 and result.record is None
    assert any("pmfa halt --resume --mode shadow" in line for line in said)
    assert patched.models[0].calls == [] and patched.feeds[0].calls == []


def test_the_live_gateway_without_a_key_is_refused_before_anything_is_read(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from pmfa.models import Gateway

    worlds: list[Any] = []

    def build(mode, config, *, review=False, models=None):  # type: ignore[no-untyped-def]
        world = ask_world(config, review=review)
        worlds.append(dataclasses.replace(world, models=Gateway(config)))
        return worlds[-1]

    monkeypatch.setattr(session_module, "build_world", build)
    monkeypatch.delenv(config.models.api_key_env, raising=False)
    result, said = _ask(config, "approve")
    assert result.exit_code == 1 and result.record is None
    assert any(line.startswith("not run: no Anthropic API key") for line in said)
    assert worlds[0].evidence._fetcher.calls == []  # type: ignore[attr-defined]


def test_a_kalshi_404_on_the_listing_is_a_plain_line(
    config: Config, patched: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pmfa.world.recorded import RecordedExchange

    def gone(self, series, *, include_closed=False):  # type: ignore[no-untyped-def]
        raise NotFound("GET /events -> 404: 404 page not found")

    monkeypatch.setattr(RecordedExchange, "list_events", gone)
    result, said = _ask(config, "approve")
    assert result.exit_code == 1 and result.record is None
    assert any(line.startswith("not run: GET /events -> 404") for line in said)


def test_the_folder_is_named_from_the_triggers_time(
    config: Config, patched: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Slow(FakeFetcher):
        """Coinbase answers, each 20 seconds after the request: the clock moves on."""

        def get(self, url: str, *, refresh: bool = False) -> Any:
            patched.worlds[-1].clock.advance(20)  # type: ignore[attr-defined]
            return super().get(url, refresh=refresh)

    monkeypatch.setattr(ask_module, "snapshot_fetcher", lambda config: Slow(coinbase_pages()))
    result, _ = _ask(config, "reject")
    stamp = result.record.trigger.trigger_id.rsplit(":", 1)[1]
    assert stamp == "20261128T052300Z" and result.folder.name == f"{stamp}-{ATM}"


def test_the_answer_and_the_reason_for_no_trade_read_truthfully(
    config: Config, patched: Built
) -> None:
    from pmfa.live.ask import answer_lines, proposal_lines

    record = _ask(config, "reject")[0].record
    close = record.forecast.model_copy(update={"market_probabilities": {ATM: 0.49998}})
    [line] = answer_lines(record.model_copy(update={"forecast": close}), market(ATM))
    assert line.startswith("The agent's answer: NO.") and "at 49.9%." in line

    # The YES side has the edge but too little resting on it; sizing then reports the
    # edge of the NO side, which meets the floor.
    step = next(t for t in record.trace if t.node == "size")
    candidates = [
        {"side": "yes", "edge": "0.1500", "liquidity": "400.00", "meets_liquidity": False},
        {"side": "no", "edge": "-0.215", "liquidity": "900.00", "meets_liquidity": True},
    ]
    thin = record.model_copy(
        update={
            "proposal": None,
            "exit": Exit.NO_EDGE,
            "exit_detail": "best edge after fees is below the minimum",
            "trace": [*record.trace, step.model_copy(update={"data": {"candidates": candidates}})],
        }
    )
    [line] = proposal_lines(thin, config)
    assert "YES side has the edge ($0.1500" in line and "too thin" in line
    assert "$400.00 rests" in line and "$500.00 floor" in line and "no_edge" not in line


def test_a_new_snapshot_replaces_the_earlier_ones_in_retrieval(
    config: Config, patched: Built
) -> None:
    _ask(config, "reject")
    patched.at = AS_OF + timedelta(minutes=5)
    result, _ = _ask(config, "reject")
    kept = [o for o in result.record.origins if o.source == COINBASE_SOURCE]
    assert [o.published_at for o in kept] == [AS_OF + timedelta(minutes=5)]
    # The store still holds both snapshots: nothing is rewritten.
    corpus = CorpusStore(live_corpus_dir(config))
    assert len([d for d in corpus.documents() if d.doc_id.startswith("spot:BTC-USD:")]) == 2
