"""``pmfa replay`` and ``pmfa trace``: print an episode from trigger to decision.

``pmfa replay PATH`` runs one recorded episode offline (no account, key or
network) and prints its full trace: a header saying what the episode is and
where it came from, every trace step in order, the forecast against the market
price with each cited passage's source, URL and publish time, the proposal, the gate's checks, the routing tier, the review card and
decision, the orders and fills, and the exit. ``pmfa trace EPISODE_ID`` prints
a stored episode from a state database in the same format.

Output goes through rich; piped to a file it has no colour codes and a fixed
width, so it reads the same in a terminal and in a text file.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from rich.console import Console

    from pmfa.examples import ReplayResult
    from pmfa.records.decision import ReviewCard
    from pmfa.records.episode import EpisodeRecord, LedgerEntry, TraceStep
    from pmfa.world.recorded import RecordedWorldFile


PIPED_WIDTH = 100
"""Line width when the output is not a terminal."""


def _console() -> Console:
    """A terminal console, or one that records plain text when the output is piped."""
    from rich.console import Console

    if sys.stdout.isatty():
        return Console(highlight=False)
    return Console(
        file=io.StringIO(), highlight=False, width=PIPED_WIDTH, no_color=True, color_system=None
    )


def _flush(console: Console) -> None:
    """Write a piped console's text with trailing spaces trimmed (rich pads table cells)."""
    buffer = console.file
    if isinstance(buffer, io.StringIO):
        lines = buffer.getvalue().splitlines()
        sys.stdout.write("".join(line.rstrip() + "\n" for line in lines))
        sys.stdout.flush()


# --------------------------------------------------------------------------- rendering


def _money(value: Any) -> str:
    return f"${value}"


def render_header(console: Console, result: ReplayResult) -> None:
    """What the episode is and its provenance; a hand-made fixture says so before anything else."""
    from pmfa.examples import HAND_MADE_NOTICE

    file = result.file
    assert file is not None and result.paths is not None
    if file.provenance == "hand-made fixture":
        console.print(f"[bold]{HAND_MADE_NOTICE}[/bold]")
    else:
        sources = sorted({e.source for e in result.cassette.entries}) if result.cassette else []
        console.print(
            "[bold]Recorded episode: the world and the model answers were recorded from a "
            f"real run (model answers from: {', '.join(sources) or 'no model calls'}).[/bold]"
        )
    console.print()
    _facts(
        console,
        [
            ("Example", result.paths.name),
            ("What it is", file.description),
            ("Provenance", file.provenance),
            ("Inputs", _shown(result.paths.world)),
            ("Model records", _shown(result.paths.cassette)),
        ],
    )
    console.print()


def _shown(path: Path) -> str:
    """A path relative to the working directory when it is under it."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _facts(console: Console, rows: list[tuple[str, str]]) -> None:
    """Label and value pairs, with wrapped values indented under their value column."""
    from rich.markup import escape
    from rich.table import Table

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(ratio=1)
    for label, value in rows:
        grid.add_row(label, escape(value))
    console.print(grid)


def render_episode(
    console: Console,
    episode: EpisodeRecord,
    *,
    card: ReviewCard | None,
    ledger: list[LedgerEntry],
    file: RecordedWorldFile | None = None,
    verbose: bool = False,
) -> None:
    """The episode in reading order: identity, trace, forecast, decision, orders, exit."""
    from rich.rule import Rule

    trigger = episode.trigger
    target = f"document {trigger.doc_id}" if trigger.doc_id else f"event {trigger.event_ticker}"
    rows = [
        ("Episode", f"{episode.episode_id} ({episode.mode.value} mode)"),
        (
            "Trigger",
            f"{trigger.kind.value}: {target}" + (f". {trigger.detail}" if trigger.detail else ""),
        ),
        ("As of", episode.as_of.isoformat()),
        ("Event", episode.event_ticker or "none matched"),
    ]
    _facts(console, rows)

    console.print(Rule("Trace: thought, action, observation"))
    render_trace(console, episode.trace, verbose=verbose)

    console.print(Rule("Forecast"))
    render_forecast(console, episode, ledger, file)

    console.print(Rule("Decision"))
    render_decision(console, episode, card)

    console.print(Rule("Orders and fills"))
    render_orders(console, episode)

    console.print(Rule("Exit"))
    exit_name = episode.exit.value if episode.exit else f"none yet ({episode.status.value})"
    console.print(f"[bold]{exit_name}[/bold]  {episode.exit_detail}")
    usage = episode.usage
    console.print(
        f"Model calls: {usage.calls}. Tokens in/out: {usage.input_tokens}/{usage.output_tokens}. "
        f"Cost: ${usage.cost_usd}. Fill tagged: {'yes' if episode.fill_tagged else 'no'}."
    )


def render_trace(console: Console, steps: list[TraceStep], *, verbose: bool = False) -> None:
    from rich.markup import escape
    from rich.table import Table

    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    for step in steps:
        text = escape(step.summary)
        if step.call is not None:
            call = step.call
            text += (
                f"\n[dim]{call.role} · {call.model} · effort {call.effort} · "
                f"answer from {call.source}[/dim]"
            )
        elif step.role:
            text += f"\n[dim]{step.role}[/dim]"
        grid.add_row(str(step.seq), step.node, step.kind, text)
        if verbose and step.data:
            grid.add_row("", "", "", escape(json.dumps(step.data, indent=2, ensure_ascii=False)))
    console.print(grid)


def _labels(file: RecordedWorldFile | None) -> dict[str, str]:
    if file is None:
        return {}
    return {m.ticker: m.subtitle or m.title for e in file.events for m in e.markets}


def is_last_trade(file: RecordedWorldFile | None, ticker: str, price: float) -> bool | None:
    """Whether ``price``, the forecast-time price of ``ticker``, is a last trade, not a YES mid.

    The pipeline takes the YES mid of the market's trigger-time book when the book
    has both sides, else the market's own quote's mid when that has both sides,
    else the last trade. This repeats that choice from the episode's recorded
    inputs, so the forecast table can say which rows are trades. ``None`` when it
    cannot tell: no recorded world (a stored episode), no such market, or a
    recomputed price that does not match the one shown.
    """
    if file is None:
        return None
    market = next((m for e in file.events for m in e.markets if m.ticker == ticker), None)
    if market is None:
        return None
    book = file.order_books.get(ticker)
    mid = book.mid_yes() if book is not None else None
    if mid is None:
        bid, ask = market.yes_bid, market.yes_ask
        if bid is not None and ask is not None and bid > 0 and ask < 1:
            mid = (bid + ask) / 2
    expected = mid if mid is not None else market.last_price
    if expected is None or abs(float(expected) - price) > 1e-9:
        return None
    return mid is None


LAST_TRADE_MARK = "*"
"""Trails a market price that is the last trade because one side of the quote is empty."""


def render_forecast(
    console: Console,
    episode: EpisodeRecord,
    ledger: list[LedgerEntry],
    file: RecordedWorldFile | None,
) -> None:
    from rich import box
    from rich.table import Table

    forecast = episode.forecast
    if forecast is None:
        console.print("No forecast was made.")
        return
    prices = {e.market_ticker: e.market_price for e in ledger}
    labels = _labels(file)
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    table.add_column("Market")
    table.add_column("Option")
    table.add_column("Forecast", justify="right")
    table.add_column("Market price at as-of", justify="right")
    # The mark sits in a column of its own, so the digits of every row stay aligned.
    table.add_column("", no_wrap=True)
    sources: list[bool | None] = []
    for ticker, probability in forecast.market_probabilities.items():
        price = prices.get(ticker)
        trade = None if price is None else is_last_trade(file, ticker, price)
        if price is not None:
            sources.append(trade)
        table.add_row(
            ticker,
            labels.get(ticker, ""),
            f"{probability:.3f}",
            "n/a" if price is None else f"{price:.3f}",
            LAST_TRADE_MARK if trade else "",
        )
    console.print(table)
    if any(sources):
        price_note = (
            "Market price is the YES mid, or the last trade where one side of the quote is "
            f"empty (marked {LAST_TRADE_MARK}); no role saw it."
        )
    elif all(s is False for s in sources):
        price_note = "Market price is the YES mid; no role saw it."
    else:
        price_note = (
            "Market price is the YES mid, or the last trade where one side of the quote is "
            "empty; no role saw it."
        )
    console.print(
        f"Coverage {forecast.coverage:.2f}, confidence {forecast.confidence:.2f}, "
        f"{forecast.n_independent_origins} independent origin(s), depth reached "
        f"{forecast.depth_reached}{', budget exhausted' if forecast.budget_exhausted else ''}"
        f"{', fast path' if forecast.fast_path else ''}. {price_note}"
    )
    if forecast.passage_ids:
        render_citations(console, forecast.passage_ids, episode.trace)


def cited_passages(trace: list[TraceStep]) -> dict[str, dict[str, Any]]:
    """Source, URL and publish time of each passage the trace's retrieval steps observed.

    The forecast record stores only passage ids; the research step's observation
    records each retrieved passage with its source, URL and publish time, so the
    citations are read back from there.
    """
    found: dict[str, dict[str, Any]] = {}
    for step in trace:
        passages = (step.data or {}).get("passages")
        if not isinstance(passages, list):
            continue
        for passage in passages:
            if isinstance(passage, dict) and isinstance(passage.get("passage_id"), str):
                found.setdefault(passage["passage_id"], passage)
    return found


def render_citations(console: Console, passage_ids: list[str], trace: list[TraceStep]) -> None:
    """One citation per passage the forecast rests on: id, source, publish time, then URL."""
    from rich.markup import escape

    known = cited_passages(trace)
    console.print("Cited passages:")
    for passage_id in passage_ids:
        passage = known.get(passage_id)
        if passage is None:
            console.print(f"  - {escape(passage_id)} (not in this trace's retrieval steps)")
            continue
        console.print(
            f"  - {escape(passage_id)} ({escape(str(passage.get('source', '')))}, "
            f"published {escape(str(passage.get('published_at', '')))})"
        )
        # soft_wrap: a URL is printed whole on its own line, never folded.
        console.print(f"    {escape(str(passage.get('url', '')))}", soft_wrap=True)


def render_decision(console: Console, episode: EpisodeRecord, card: ReviewCard | None) -> None:
    from rich.markup import escape
    from rich.table import Table

    p = episode.proposal
    if p is None:
        console.print("No proposal: nothing was sized.")
    else:
        console.print(
            f"[bold]Proposal[/bold] {p.proposal_id}: buy {p.contracts} {p.side.value.upper()} "
            f"on {p.market_ticker} at no more than {_money(p.limit_price)} "
            f"(immediate-or-cancel limit)."
        )
        console.print(
            f"  forecast P({p.side.value}) {p.forecast_probability:.3f}, shrunk "
            f"{p.shrunk_probability:.3f}, best ask {_money(p.reference_price)}, edge after fees "
            f"{p.edge:.4f}, edge bound {_money(p.edge_bound_price)}"
        )
        console.print(
            f"  fee {_money(p.fee)}, worst-case cost {_money(p.cost)}, confidence "
            f"{p.confidence:.2f}, bankroll {_money(p.bankroll)}, position "
            f"{p.position_before} -> {p.position_target}, expires {p.expires_at.isoformat()}"
        )
    gate = episode.gate
    if gate is not None:
        verdict = "approved" if gate.approved else "rejected: " + ", ".join(gate.reasons)
        console.print(f"[bold]Gate[/bold] {verdict}")
        table = Table(box=None, show_edge=False, pad_edge=False, show_header=False)
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column()
        for check in gate.checks:
            table.add_row("pass" if check.passed else "FAIL", check.name, escape(check.detail))
        console.print(table)
    routing = episode.routing
    if routing is not None:
        reasons = ", ".join(routing.reasons) or "every auto condition held"
        console.print(
            f"[bold]Routing[/bold] tier {routing.tier.value} (risk score {routing.score}; "
            f"{reasons}){'; would be auto at the autonomous stage' if routing.would_be_auto else ''}"
        )
    if card is not None:
        console.print(f"[bold]Review card[/bold] for {card.proposal_id}")
        console.print(f"  {escape(card.event_title)} / {escape(card.market_title)}")
        console.print(
            f"  buy {card.contracts} {card.side.value.upper()} at {_money(card.limit_price)}, "
            f"cost {_money(card.cost)}; forecast {card.forecast_probability:.3f} against "
            f"price {card.market_price}; edge {card.edge:.4f}; confidence {card.confidence:.2f}"
        )
        console.print(f"  routing reasons: {', '.join(card.routing_reasons) or 'none'}")
        for passage in card.passages:
            console.print(
                f"  - {passage.passage_id} ({passage.source}, "
                f"{passage.published_at.isoformat()}): {escape(passage.excerpt)}"
            )
        console.print(f"  expires {card.expires_at.isoformat()}")
    review = episode.review
    if review is not None:
        size = f" to {review.contracts} contracts" if review.contracts is not None else ""
        reason = f", reason {review.reason_code.value}" if review.reason_code else ""
        note = f": {escape(review.note)}" if review.note else ""
        console.print(
            f"[bold]Review[/bold] {review.reviewer} chose {review.decision.value}{size}{reason} "
            f"at {review.decided_at.isoformat()}{note}"
        )


def render_orders(console: Console, episode: EpisodeRecord) -> None:
    from pmfa.live.text import unknown_outcome_sentence
    from pmfa.records.episode import EpisodeStatus

    if episode.status is EpisodeStatus.OUTCOME_UNKNOWN:
        console.print(
            "No order is recorded, and its outcome is unknown. "
            + unknown_outcome_sentence(episode.mode)
        )
        return
    if not episode.orders:
        done = episode.status is EpisodeStatus.DONE
        console.print("No order was sent." if done else "No order is recorded yet.")
        return
    for order in episode.orders:
        console.print(
            f"Order {order.order_id}: {order.requested_contracts} {order.side.value.upper()} on "
            f"{order.market_ticker} at limit {_money(order.limit_price)}: {order.status.value}, "
            f"{order.filled_contracts} filled, fees {_money(order.fees)}"
            + (f" ({order.error})" if order.error else "")
        )
        for fill in order.fills:
            console.print(
                f"  fill {fill.fill_id}: {fill.contracts} at {_money(fill.price)}, fee "
                f"{_money(fill.fee)}, {fill.filled_at.isoformat()}"
                f"{' (simulated)' if fill.simulated else ''}"
            )


# --------------------------------------------------------------------------- commands


def replay_command(
    path: str | None = typer.Argument(
        None, help="An example directory, a world file, or the name of a committed example."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the episode record as JSON instead."),
    verbose: bool = typer.Option(False, "--verbose", help="Also print each step's records."),
    check: bool = typer.Option(
        False,
        "--check",
        help="Compare with the example's expected/ files; non-zero on a difference.",
    ),
    list_examples: bool = typer.Option(
        False, "--list", help="List the committed examples, one line each."
    ),
) -> None:
    """Replay a recorded episode offline (no account, key or network) and print its trace."""
    from pmfa import examples
    from pmfa.errors import CassetteMiss
    from pmfa.world.recorded import RecordedKnowledgeMiss

    if list_examples:
        found = examples.list_examples()
        if not found:
            root = examples.examples_root()
            hint = "" if root.is_dir() else f"\n{examples.NO_CHECKOUT_HINT}"
            typer.echo(f"no examples under {root}{hint}")
            raise typer.Exit(1)
        for directory in found:
            typer.echo(examples.summary_line(directory))
        return
    if path is None:
        raise typer.BadParameter("give an example directory or world file, or use --list")
    try:
        result = examples.replay(path)
    except examples.ExampleError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    except (CassetteMiss, RecordedKnowledgeMiss) as exc:
        typer.echo(
            f"replay stopped: {exc}\nThe recording no longer matches what the pipeline asks "
            "for, so the replay is not faithful. Rebuild the example "
            "(python scripts/build_examples.py) if the change is intended.",
            err=True,
        )
        raise typer.Exit(3) from None

    if as_json:
        typer.echo(result.episode.model_dump_json(indent=2))
    else:
        console = _console()
        render_header(console, result)
        render_episode(
            console,
            result.episode,
            card=result.card,
            ledger=result.ledger,
            file=result.file,
            verbose=verbose,
        )
        _flush(console)
    if check:
        problems = examples.check(result)
        if problems:
            for problem in problems:
                typer.echo(f"check failed: {problem}", err=True)
            raise typer.Exit(1)
        typer.echo("check passed: the replay matches expected/ exactly", err=as_json)


def trace_command(
    episode_id: str = typer.Argument(..., help="The episode to print."),
    db: str | None = typer.Option(
        None, "--db", help="The state database. Default: the configured data/state.sqlite."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the episode record as JSON instead."),
    verbose: bool = typer.Option(False, "--verbose", help="Also print each step's records."),
) -> None:
    """Print a stored episode's trace from a state database, in the replay's format."""
    from pmfa.state.store import SqliteStore

    if db is None:
        from pmfa.config import load_config

        database = load_config().paths.state_db()
    else:
        database = Path(db)
    if not database.is_file():
        typer.echo(f"error: no state database at {database}", err=True)
        raise typer.Exit(2)
    store = SqliteStore(database)
    episode = store.get_episode(episode_id)
    if episode is None:
        typer.echo(f"error: no episode {episode_id!r} in {database}", err=True)
        raise typer.Exit(2)
    if as_json:
        typer.echo(episode.model_dump_json(indent=2))
        return
    card = None
    if episode.proposal is not None:
        stored = store.get_proposal(episode.proposal.proposal_id)
        card = stored[2] if stored is not None else None
    ledger = [e for e in store.ledger() if e.episode_id == episode_id]
    console = _console()
    console.print(f"[bold]Stored episode from {database}[/bold]")
    console.print()
    render_episode(console, episode, card=card, ledger=ledger, verbose=verbose)
    _flush(console)


def register(app: typer.Typer) -> None:
    app.command("replay")(replay_command)
    app.command("trace")(trace_command)


__all__ = [
    "LAST_TRADE_MARK",
    "cited_passages",
    "is_last_trade",
    "register",
    "render_citations",
    "render_decision",
    "render_episode",
    "render_forecast",
    "render_header",
    "render_orders",
    "render_trace",
    "replay_command",
    "trace_command",
]
