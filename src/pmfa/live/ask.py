"""``pmfa ask``: one shadow-mode episode now, on one market of one series, decided by the person.

It is shadow mode, not a new run mode: live production prices read-only, live
evidence, live models, a paper fill, nothing sent to any exchange. One run, in
order (:func:`run_ask`, each step reported through ``say``):

1. Open the shadow session with review on, on the loaded configuration with
   the asked series switched on in memory (:func:`asked_config`). Refused
   before anything is read: a halted shadow mode (:func:`check_halt`), a shadow
   stage at ``autonomous``, since routing could then place an order without
   asking (:func:`check_stage`), and the live gateway without an API key
   (:func:`check_api_key`).
2. Pick the event: the series' open event that closes soonest among those
   closing at least ``min_minutes`` from now and within the fast-path window,
   so the episode takes the fast path (:func:`choose_event`); or the event of
   ``--market``.
3. Pick the market: ``--market``, else the one whose YES mid is nearest 0.50
   (:func:`choose_market`), and focus the world on it
   (``pmfa.world.focus``): the event then has that one market, so the ladder
   outcome space has two outcomes.
4. Gather the sources as of now: the live evidence is polled and indexed as a
   shadow pass does it, with the series' feeds among the feeds read, and, for
   a series with a ``spot_product``, the Coinbase price snapshot is read, stored
   and indexed, replacing earlier snapshots in the index (:func:`gather_sources`).
   A source that cannot be read is a note.
5. Run one episode through the controller, on a schedule-kind trigger that names
   the event (:func:`asked_trigger`), so the match step makes no model call.
6. When it pauses for review, ask the person (``decide``) and resume it through
   ``pmfa.live.session.decide``, the code ``pmfa review`` uses. The proposal is
   decided in the run, never left for later (see ``Decide``).
7. Write ``runs/ask/<time>-<market>/``: the kept sources, ``answer.md`` and
   ``episode.json`` (:func:`write_folder`), whatever the episode's exit.

No role sees the market's price: it is read only to tell the person, after the
episode, and never enters a document or a role's input. Times come from the
world's clock. The functions here print nothing; the command echoes ``say``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pmfa.errors import ConfigError, ExchangeError, NotFound, PmfaError
from pmfa.records.base import RunMode, Stage
from pmfa.records.decision import ReasonCode, ReviewCard, ReviewDecision
from pmfa.records.episode import EpisodeRecord, EpisodeStatus, Exit
from pmfa.records.evidence import Document, Trigger, TriggerKind
from pmfa.records.market import Event, Market, MarketStatus, Side

if TYPE_CHECKING:
    from pmfa.config import Config, SeriesConfig
    from pmfa.episode import EpisodeController
    from pmfa.ports import StateStore, World
    from pmfa.world.evidence.http import Fetcher

ASKED = "asked by the operator"
"""The detail of an asked episode's trigger."""
ASK_DIR = "ask"
"""The folder under ``paths.runs_dir`` that holds one folder per asked run."""
REJECT_NOTE = "rejected in pmfa ask"

Say = Callable[[str], None]
Choice = tuple[ReviewDecision, int | None]
"""A person's decision: approve, reject, or resize to a number of contracts."""
Decide = Callable[[ReviewCard], Choice]
"""Shown the review card, returns the person's decision. An asked proposal is always decided in
the run: ``pmfa review`` runs on the loaded configuration, where the asked series may be off,
so its gate would refuse to approve it later."""


class AskError(PmfaError):
    """What an asked run cannot do, in plain words. Nothing was spent when it is raised."""


# --------------------------------------------------------------------------- configuration


def asked_config(config: Config, series_ticker: str) -> Config:
    """``config`` with ``series_ticker`` switched on in memory, and nothing else changed.

    Raises ``ConfigError`` naming the configured series when it is not one of them.
    """
    found = config.follow.for_ticker(series_ticker)
    if found is None:
        names = ", ".join(s.ticker for s in config.follow.series)
        raise ConfigError(f"{series_ticker!r} is not a configured series (configured: {names})")
    if found.enabled:
        return config
    series = [
        s.model_copy(update={"enabled": True}) if s.ticker == series_ticker else s
        for s in config.follow.series
    ]
    return config.model_copy(update={"follow": config.follow.model_copy(update={"series": series})})


def check_halt(store: StateStore) -> None:
    """Refuse to run while shadow mode is halted: every order would be refused after the
    sources were read and the models paid for."""
    halt = store.get_halt()
    if halt.halted:
        raise AskError(
            f"shadow mode is halted ({halt.reason or 'no reason given'}), so no order could be "
            "placed. pmfa halt --status --mode shadow shows why; pmfa halt --resume --mode "
            "shadow clears it."
        )


def check_api_key(world: World) -> None:
    """Refuse before any fetch when the live gateway has no API key to call with.

    Only whether the variable is set is read; its value is never read here or shown.
    """
    import os

    from pmfa.models.gateway import Gateway

    name = world.config.models.api_key_env
    if isinstance(world.models, Gateway) and not os.environ.get(name):
        raise AskError(
            f"no Anthropic API key: set the environment variable {name} first "
            "(docs/demo.md shows how). Nothing was read and no model was called."
        )


def check_stage(store: StateStore) -> None:
    """Refuse an autonomous shadow stage: an asked proposal must always wait for the person.

    Routing sends a proposal to the auto tier only at the autonomous stage, and the
    review policy of the session pauses every other one, so below that stage every
    asked proposal waits. At that stage nothing is weakened: the run is refused.
    """
    stage = store.get_stage().stage
    if stage is Stage.AUTONOMOUS:
        raise AskError(
            "the shadow stage is autonomous, so a proposal could be filled without asking you; "
            "pmfa ask needs every proposal to wait for you. Lower it first with "
            'pmfa stage demote --reason "asking by hand" --mode shadow.'
        )


# --------------------------------------------------------------------------- choosing


def minutes_until(moment: datetime, now: datetime) -> int:
    """Whole minutes from ``now`` to ``moment``, rounded down."""
    return int((moment - now).total_seconds() // 60)


def choose_event(
    events: list[Event], now: datetime, *, min_minutes: int, window_minutes: int
) -> Event:
    """The open event closing soonest among those closing ``min_minutes`` to ``window_minutes``
    from ``now`` (ties to the lowest ticker). Raises ``AskError`` saying why there is none."""
    low, high = timedelta(minutes=min_minutes), timedelta(minutes=window_minutes)
    fits = [e for e in events if e.markets and low <= e.close_time - now <= high]
    if fits:
        return min(fits, key=lambda e: (e.close_time, e.event_ticker))
    later = sorted((e for e in events if e.markets and e.close_time > now), key=_closing)
    hint = " Or pass --market TICKER to name a market explicitly."
    unlisted = (
        " A series that opens an event every hour lists the next one shortly after the hour: "
        "try again in a minute."
    )
    if not later:
        raise AskError("the series has no open event right now." + unlisted + hint)
    first = later[0]
    left = minutes_until(first.close_time, now)
    if first.close_time - now < low:
        after = next((e for e in later if e.close_time - now > high), None)
        raise AskError(
            f"the current event {first.event_ticker} closes at {first.close_time:%H:%M} UTC, in "
            f"{left} minute(s): less than the {min_minutes} minutes a run needs. The next one "
            f"opens when it closes; run pmfa ask again after {first.close_time:%H:%M} UTC."
            + (
                f" (The next open event, {after.event_ticker}, closes later than the "
                f"{window_minutes}-minute fast-path window.)"
                if after
                else ""
            )
            + hint
        )
    raise AskError(
        f"no open event closes within {window_minutes} minutes: the soonest, "
        f"{first.event_ticker}, closes at {first.close_time:%Y-%m-%d %H:%M} UTC, in {left} "
        "minute(s)."
        + unlisted
        + " Or pass --market TICKER to name a market of a later event (its episode then runs "
        "the full scenario tree)."
    )


def _closing(event: Event) -> tuple[datetime, str]:
    return event.close_time, event.event_ticker


def quoted_mid(market: Market) -> Decimal | None:
    """The YES mid when both a bid and an ask are quoted, else ``None``."""
    bid, ask = market.yes_bid, market.yes_ask
    if bid is None or ask is None or bid <= 0 or ask >= 1:
        return None
    return (bid + ask) / 2


def choose_market(event: Event) -> Market:
    """The open market whose quoted YES mid is nearest 0.50, ties to the lowest ticker."""
    quoted = [
        (abs(mid - Decimal("0.5")), m.ticker, m)
        for m in event.markets
        if m.status is MarketStatus.OPEN and (mid := quoted_mid(m)) is not None
    ]
    if not quoted:
        raise AskError(
            f"no market of {event.event_ticker} has both a bid and an ask right now, so there is "
            "no price to compare a forecast with. Try again in a minute, or pass --market TICKER."
        )
    return min(quoted, key=lambda q: (q[0], q[1]))[2]


def market_and_event(
    world: World, ticker: str, series_ticker: str, now: datetime
) -> tuple[Event, Market]:
    """The open market ``ticker`` of the asked series, and its event. Raises ``AskError``
    when it is not open or belongs to another series."""
    try:
        market = world.exchange.get_market(ticker)
        event = world.exchange.get_event(market.event_ticker)
    except NotFound:
        raise AskError(f"Kalshi lists no market {ticker!r}.") from None
    if event.series_ticker != series_ticker:
        raise AskError(f"market {ticker} belongs to {event.series_ticker}, not {series_ticker}.")
    listed = next((m for m in event.markets if m.ticker == ticker), market)
    if listed.status is not MarketStatus.OPEN or listed.close_time <= now:
        raise AskError(f"market {ticker} is not open (it is {listed.status.value}).")
    return event, listed


# --------------------------------------------------------------------------- sources


def snapshot_fetcher(config: Config) -> Fetcher:
    """The fetcher the price snapshot is read through (the seam tests replace)."""
    from pmfa.world.evidence.http import PoliteFetcher

    return PoliteFetcher.from_config(config)


@dataclass
class Gathered:
    """What the gathering step read: documents visible now, the snapshot, passages indexed."""

    documents: int = 0
    passages: int = 0
    snapshot: Document | None = None
    notes: list[str] = field(default_factory=list)


def gather_sources(world: World, series: SeriesConfig, fetcher: Fetcher) -> Gathered:
    """Poll the live evidence and index what is visible now, as a shadow pass does, then read,
    store and index the series' price snapshot in place of earlier ones. A source that cannot
    be read is a note."""
    from pmfa.live.loop import _index, _new_documents
    from pmfa.world.evidence.spot import snapshot_document

    out = Gathered()
    now = world.clock.now()
    try:
        documents = _new_documents(world, None, now, out.notes)
    except PmfaError as exc:
        out.notes.append(f"evidence not read: {exc}")
        documents = []
    if series.spot_product:
        # Earlier runs' snapshots leave the index (the store keeps them), whether or not a new
        # one is read: they would fill the retrieval slots, and a stale price is no evidence.
        prefix = f"spot:{series.spot_product}:"
        older = [d.doc_id for d in documents if d.doc_id.startswith(prefix)]
        documents = [d for d in documents if d.doc_id not in older]
        remove = getattr(world.knowledge, "remove_documents", None)
        if older and callable(remove):
            remove(older)
        try:
            out.snapshot = snapshot_document(
                fetcher,
                series.spot_product,
                base_url=world.config.sources.coinbase_url,
                clock=world.clock,
            )
        except PmfaError as exc:
            out.notes.append(f"price snapshot of {series.spot_product} not read: {exc}")
        else:
            add = getattr(world.evidence, "add", None)
            if callable(add):
                add([out.snapshot])
            documents.append(out.snapshot)
    out.documents = len(documents)
    out.passages = _index(world.knowledge, "index_documents", documents)
    return out


def asked_trigger(event_ticker: str, at: datetime) -> Trigger:
    """A schedule-kind trigger naming the event: the match step makes no model call for it."""
    return Trigger(
        trigger_id=f"schedule:{event_ticker}:asked:{at:%Y%m%dT%H%M%SZ}",
        kind=TriggerKind.SCHEDULE,
        at=at,
        event_ticker=event_ticker,
        detail=ASKED,
    )


@dataclass(frozen=True)
class KeptSource:
    """One document the episode kept passages of, and which of its passages."""

    document: Document
    passages: tuple[int, ...]
    primary: bool


def kept_passage_ids(record: EpisodeRecord) -> list[str]:
    """The passages the episode kept: the record's, else the research step's last selection
    (an episode that ended at the origin rule keeps none in its record)."""
    if record.passage_ids:
        return list(record.passage_ids)
    for step in reversed(record.trace):
        if step.node == "research" and isinstance(step.data.get("passage_ids"), list):
            return [str(p) for p in step.data["passage_ids"]]
    return []


def kept_sources(world: World, record: EpisodeRecord, series: SeriesConfig) -> list[KeptSource]:
    """The documents behind the kept passages, in the order first cited."""
    from pmfa.knowledge.origins import primary_sources

    by_doc: dict[str, list[int]] = {}
    for pid in kept_passage_ids(record):
        doc_id, _, n = pid.rpartition("#")
        by_doc.setdefault(doc_id or pid, []).append(int(n) if n.isdigit() else 0)
    primaries = primary_sources(world.config, series.category)
    out = []
    for doc_id, indices in by_doc.items():
        try:
            doc = world.evidence.get_document(doc_id, as_of=record.as_of)
        except NotFound:
            continue
        out.append(KeptSource(doc, tuple(sorted(set(indices))), doc.source in primaries))
    return out


# --------------------------------------------------------------------------- the answer


def probability_yes(record: EpisodeRecord, market_ticker: str) -> float | None:
    if record.forecast is None:
        return None
    return record.forecast.market_probabilities.get(market_ticker)


def _price(value: Decimal | None) -> str:
    return "no offer" if value is None else f"${value:.2f}"


def prices_now(world: World, market_ticker: str) -> tuple[Decimal | None, Decimal | None]:
    """What one YES and one NO contract cost now: the best offer on each side."""
    book = world.executor.execution_book(market_ticker)
    yes, no = book.best_ask(Side.YES), book.best_ask(Side.NO)
    return (yes.price if yes else None, no.price if no else None)


def answer_lines(record: EpisodeRecord, market: Market) -> list[str]:
    """The agent's answer, YES or NO, with its probability; or why it has none."""
    p = probability_yes(record, market.ticker)
    label = market.subtitle or market.title
    if p is None:
        end = record.exit.value if record.exit else record.status.value
        return [
            f"The agent has no answer: the episode ended {end} before a forecast was made"
            + (f" ({record.exit_detail})." if record.exit_detail else ".")
        ]
    word = "YES" if p >= 0.5 else "NO"
    # Down to a tenth of a percent, and below 50 for NO: 0.49998 reads 49.9%, never 50%.
    percent = math.floor(p * 1000 + 1e-9) / 10
    if p < 0.5:
        percent = min(percent, 49.9)
    return [f"The agent's answer: {word}. It puts the chance of YES ({label}) at {percent:.1f}%."]


def thin_side_line(record: EpisodeRecord, config: Config) -> str | None:
    """Why there was no trade when the side with the best edge had too little resting on it.

    Sizing picks among the sides that meet the liquidity floor, so its reason then names
    the edge of a side the agent did not favour. This reads the size step's candidates.
    """
    step = next((t for t in reversed(record.trace) if t.node == "size"), None)
    raw = step.data.get("candidates") if step is not None else None
    if not isinstance(raw, list) or not raw:
        return None
    best = max(raw, key=lambda c: Decimal(str(c["edge"])))
    edge = Decimal(str(best["edge"]))
    if best["meets_liquidity"] or edge < Decimal(str(config.sizing.min_edge)):
        return None
    return (
        f"The {str(best['side']).upper()} side has the edge (${edge:.4f} a contract after fees), "
        f"but its offers are too thin to trade: ${Decimal(str(best['liquidity'])):.2f} rests near "
        f"the best price, below the ${config.caps.liquidity_floor_dollars:.2f} floor."
    )


def proposal_lines(record: EpisodeRecord, config: Config) -> list[str]:
    """The proposal in words, or why there is none."""
    p = record.proposal
    if p is None:
        if record.forecast is None:
            return []
        end = record.exit.value if record.exit else record.status.value
        thin = thin_side_line(record, config) if record.exit is Exit.NO_EDGE else None
        if thin is not None:
            return [f"No trade proposed: {thin}"]
        return [
            f"No trade proposed: {end}" + (f": {record.exit_detail}" if record.exit_detail else "")
        ]
    return [
        f"Proposal: buy {p.contracts} {p.side.value.upper()} contract(s) at ${p.limit_price:.2f} "
        "or less (a limit set from the order book when the proposal was made). The most it can "
        f"cost is ${p.cost:.2f}, including an estimated fee of ${p.fee:.2f}; the edge after fees "
        f"is ${p.edge:.4f} a contract."
    ]


def source_lines(sources: list[KeptSource]) -> list[str]:
    if not sources:
        return ["Sources kept: none."]
    lines = [f"Sources kept: {len(sources)}"]
    for i, s in enumerate(sources, 1):
        d = s.document
        kind = "primary" if s.primary else "secondary"
        lines.append(
            f"  {i}. [{kind}] {d.title} ({d.source}, published {d.published_at:%Y-%m-%d %H:%M} "
            f"UTC) {d.url}"
        )
    return lines


# --------------------------------------------------------------------------- the folder


def _slug(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "document"


def marked_text(document: Document, used: tuple[int, ...], config: Config) -> str:
    """The document's text, with the words of the passages the agent used in bold.

    The passages are found the way the index chunked the document
    (``pmfa.knowledge.chunking``), so the marks cover exactly what was retrieved.
    """
    from pmfa.knowledge.chunking import passage_spans

    paragraphs = [p.split() for p in re.split(r"\n\s*\n", document.text) if p.split()]
    total = sum(len(p) for p in paragraphs)
    spans = passage_spans(
        len(paragraphs[0]) if paragraphs else 0,
        total,
        passage_words=config.retrieval.passage_words,
        overlap_words=config.retrieval.passage_overlap_words,
    )
    marked = {i for n in used if n < len(spans) for i in range(*spans[n])}
    out, position = [], 0
    for words in paragraphs:
        pieces: list[str] = []
        run: list[str] = []
        bold = False
        for word in words:
            here = position in marked
            if run and here != bold:
                pieces.append(f"**{' '.join(run)}**" if bold else " ".join(run))
                run = []
            bold = here
            run.append(word)
            position += 1
        if run:
            pieces.append(f"**{' '.join(run)}**" if bold else " ".join(run))
        out.append(" ".join(pieces))
    return "\n\n".join(out)


def source_markdown(source: KeptSource, config: Config) -> str:
    d = source.document
    used = ", ".join(str(n + 1) for n in source.passages)
    return (
        f"# {d.title}\n\n"
        f"- URL: {d.url}\n"
        f"- Publisher: {d.source}\n"
        f"- Published: {d.published_at:%Y-%m-%d %H:%M:%S} UTC\n"
        f"- Origin: {'primary' if source.primary else 'secondary'}\n"
        f"- Passages the agent used: {used} (their words are in **bold** below)\n\n"
        "Evidence text is untrusted: it was shown to the roles inside delimited blocks.\n\n"
        "---\n\n" + marked_text(d, source.passages, config) + "\n"
    )


def folder_for(config: Config, at: datetime, market_ticker: str) -> Path:
    return Path(config.paths.runs_dir) / ASK_DIR / f"{at:%Y%m%dT%H%M%SZ}-{market_ticker}"


def write_folder(
    folder: Path,
    *,
    config: Config,
    question: list[str],
    sources: list[KeptSource],
    record: EpisodeRecord | None,
    lines: list[str],
    notes: list[str],
) -> Path:
    """Write the kept sources, ``answer.md`` and ``episode.json`` (when there is an episode)."""
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "sources"
    src.mkdir(exist_ok=True)
    names = []
    for i, s in enumerate(sources, 1):
        name = f"{i:02d}-{_slug(s.document.source)}-{_slug(s.document.title)}.md"
        (src / name).write_text(source_markdown(s, config), encoding="utf-8")
        names.append(name)
    body = ["# pmfa ask", "", *question, ""]
    body += [*lines, ""]
    if names:
        body += ["## Sources kept", ""]
        body += [
            f"{i}. [{n}](sources/{n}) ({'primary' if s.primary else 'secondary'})"
            for i, (n, s) in enumerate(zip(names, sources, strict=True), 1)
        ]
        body.append("")
    if notes:
        body += ["## Notes", "", *(f"- {n}" for n in notes), ""]
    if record is not None:
        body += [
            "## The full trace",
            "",
            f"`uv run pmfa trace {record.episode_id}` prints every step, and `episode.json` "
            "here holds the episode record.",
            "",
        ]
        (folder / "episode.json").write_text(
            json.dumps(record.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
    body += [
        "Shadow mode: live prices, a paper fill only, nothing sent to any exchange. Mock funds; "
        "not trading advice.",
    ]
    (folder / "answer.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- one run


@dataclass
class AskResult:
    """What one asked run did; ``exit_code`` is the command's."""

    exit_code: int
    folder: Path | None = None
    record: EpisodeRecord | None = None
    market: Market | None = None
    lines: list[str] = field(default_factory=list)


def run_ask(
    config: Config,
    series_ticker: str,
    *,
    decide: Decide,
    say: Say,
    market_ticker: str | None = None,
    min_minutes: int = 10,
) -> AskResult:
    """One asked run (see the module docstring). ``config`` is the loaded configuration.

    Returns exit code 0 when the episode ended in any exit but ``error``, 1 when it
    could not run or ended in ``error``. Raises ``ConfigError`` for an unknown series
    or a session that cannot be opened.
    """
    from pmfa.episode import EpisodeController
    from pmfa.live import session as live_session
    from pmfa.live.text import card_lines, decision_lines
    from pmfa.world.focus import focus_world

    config = asked_config(config, series_ticker)
    series = config.follow.for_ticker(series_ticker)
    assert series is not None
    window = config.forecast.fast_path_within_minutes
    with live_session.open_session(RunMode.SHADOW, config, review=True) as s:
        say(f"Shadow session open: state in {s.state_db}.")
        try:
            check_halt(s.world.store)
            check_stage(s.world.store)
            check_api_key(s.world)
            now = s.world.clock.now()
            if market_ticker:
                event, market = market_and_event(s.world, market_ticker, series.ticker, now)
                if event.close_time - now > timedelta(minutes=window):
                    say(
                        f"warning: {event.event_ticker} closes in "
                        f"{minutes_until(event.close_time, now)} minutes, beyond the "
                        f"{window}-minute fast path: the full scenario tree runs and may reach "
                        f"the {config.budget.max_seconds_per_episode:g}-second cap."
                    )
            else:
                event = choose_event(
                    s.world.exchange.list_events(series.ticker),
                    now,
                    min_minutes=min_minutes,
                    window_minutes=window,
                )
                market = choose_market(event)
        except (AskError, ExchangeError, NotFound) as exc:
            say(f"not run: {exc}")
            return AskResult(exit_code=1)
        question = [
            f"Event: {event.title} ({event.event_ticker})",
            f"Market: {market.subtitle or market.title} ({market.ticker})",
            f"Rules: {market.rules_primary}",
            f"Closes: {market.close_time:%Y-%m-%d %H:%M} UTC, in "
            f"{minutes_until(market.close_time, now)} minute(s).",
        ]
        for line in question:
            say(line)
        world = focus_world(s.world, event.event_ticker, market.ticker)
        controller = EpisodeController(world, checkpointer=s.controller.checkpointer)
        trigger: Trigger | None = None
        record: EpisodeRecord | None = None
        lines: list[str] = []
        notes: list[str] = []
        sources: list[KeptSource] = []
        try:
            say(
                f"Focused on {market.ticker}: two outcomes, YES ({market.subtitle or market.title})"
                " and NO."
            )
            fetcher = snapshot_fetcher(config)
            try:
                gathered = gather_sources(world, series, fetcher)
            finally:
                close = getattr(fetcher, "close", None)
                if callable(close):
                    close()
            notes += gathered.notes
            say(
                f"Sources gathered as of {world.clock.now():%H:%M:%S} UTC: {gathered.documents} "
                f"document(s) visible, {gathered.passages} passage(s) indexed"
                + (", price snapshot read." if gathered.snapshot else ".")
            )
            for note in gathered.notes:
                say(f"note: {note}")
            trigger = asked_trigger(event.event_ticker, world.clock.now())
            say(f"Running one episode ({trigger.trigger_id}): four short model calls.")
            record = _run(controller, world, trigger)
            sources = kept_sources(world, record, series)
            lines.append(
                f"Episode {record.episode_id}: pmfa trace {record.episode_id} prints every step."
            )
            lines += source_lines(sources)
            lines += answer_lines(record, market)
            try:
                yes, no = prices_now(world, market.ticker)
                lines.append(
                    f"Prices on Kalshi now, read after the episode: YES {_price(yes)}, NO "
                    f"{_price(no)} a contract (the agent never saw them)."
                )
            except (ExchangeError, NotFound) as exc:
                lines.append(f"note: the prices now could not be read: {exc}")
            lines += proposal_lines(record, config)
            for line in lines:
                say(line)
            if record.status is EpisodeStatus.PENDING_REVIEW and record.proposal is not None:
                card = world.store.get_proposal(record.proposal.proposal_id)
                if card is not None and card[2] is not None:
                    for line in card_lines(card[2]):
                        say(line)
                    decision, contracts = decide(card[2])
                    record = live_session.decide(
                        live_session.Session(world, controller, s.state_db, s.checkpoint_db),
                        record.proposal.proposal_id,
                        decision,
                        contracts=contracts,
                        reason_code=ReasonCode.OTHER if decision is ReviewDecision.REJECT else None,
                        note=REJECT_NOTE if decision is ReviewDecision.REJECT else "",
                    )
                    decided = [f"Your decision: {decision.value}", *decision_lines(record)]
                    lines += decided
                    for line in decided:
                        say(line)
            elif record.exit is not None and record.proposal is not None:
                ended = decision_lines(record)
                lines += ended
                for line in ended:
                    say(line)
            if record.exit is Exit.HALTED:
                halted = (
                    "Shadow mode is halted: pmfa halt --status --mode shadow shows why, and "
                    "pmfa halt --resume --mode shadow clears it."
                )
                lines.append(halted)
                say(halted)
        except live_session.NotDecided as exc:
            pid = record.proposal.proposal_id if record and record.proposal else "ID"
            pending = (
                f"{exc}. The proposal stays pending until it expires; pmfa review reject {pid} "
                "--mode shadow ends it now."
            )
            lines.append(pending)
            say(pending)
        finally:
            folder = folder_for(config, trigger.at if trigger is not None else now, market.ticker)
            write_folder(
                folder,
                config=config,
                question=question,
                sources=sources,
                record=record,
                lines=lines,
                notes=notes,
            )
            controller.close()
        say(f"Saved in {folder}: answer.md, episode.json, sources/ ({len(sources)} file(s)).")
        if record is not None and record.forecast is not None:
            say(
                f"After {market.close_time:%H:%M} UTC, once the market settles: pmfa resolve "
                "scores the forecast and settles any paper position, and pmfa report-status "
                "shows the account."
            )
    failed = record is None or record.exit is Exit.ERROR
    return AskResult(
        exit_code=1 if failed else 0, folder=folder, record=record, market=market, lines=lines
    )


def _run(controller: EpisodeController, world: World, trigger: Trigger) -> EpisodeRecord:
    """Run the episode; a run whose dollar cap was reached still returns its recorded episode."""
    from pmfa.episode import episode_id_for
    from pmfa.errors import RunBudgetExhausted

    try:
        return controller.run(trigger)
    except RunBudgetExhausted:
        stored = world.store.get_episode(episode_id_for(world.mode, trigger))
        if stored is None:
            raise
        return stored


__all__ = [
    "ASKED",
    "AskError",
    "AskResult",
    "KeptSource",
    "asked_config",
    "asked_trigger",
    "check_api_key",
    "check_halt",
    "check_stage",
    "choose_event",
    "choose_market",
    "gather_sources",
    "kept_sources",
    "marked_text",
    "run_ask",
    "write_folder",
]
