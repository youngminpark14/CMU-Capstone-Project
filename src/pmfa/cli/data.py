"""``pmfa data``: rebuild the frozen data (corpus and snapshot) and show what it holds.

``pmfa data build`` rebuilds both halves in one command: the evidence corpus
(Federal Reserve and BLS documents plus the release calendar) and the exchange
snapshot (events, markets, results and candles of the followed series). Both
go under ``config.paths`` (``data/``, ignored by git). ``pmfa data status``
prints both manifests.

Imports happen inside the commands, so ``pmfa --help`` stays fast.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from pmfa.config import Config

data_app = typer.Typer(
    name="data",
    help="Rebuild the frozen corpus and snapshot that backtests read, and show their manifests.",
    no_args_is_help=True,
)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


def _parse_day(text: str | None, option: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise typer.BadParameter(f"{text!r} is not a YYYY-MM-DD date", param_hint=option) from None


def _follow_only(config: Config, tickers: list[str]) -> Config:
    """``config`` with exactly ``tickers`` enabled among the followed series."""
    known = {s.ticker for s in config.follow.series}
    unknown = [t for t in tickers if t not in known]
    if unknown:
        raise typer.BadParameter(
            f"not a followed series: {', '.join(unknown)} (known: {', '.join(sorted(known))})",
            param_hint="--series",
        )
    series = [s.model_copy(update={"enabled": s.ticker in tickers}) for s in config.follow.series]
    follow = config.follow.model_copy(update={"series": series})
    return config.model_copy(update={"follow": follow})


@data_app.command("build")
def build(
    start: str | None = typer.Option(
        None, help="Window start, YYYY-MM-DD. Default: evaluation.window_start."
    ),
    end: str | None = typer.Option(
        None, help="Last close date to include, YYYY-MM-DD. Default: now."
    ),
    series: Annotated[
        list[str] | None,
        typer.Option(
            "--series", help="A series to build (repeatable). Default: every enabled series."
        ),
    ] = None,
    minute_days: int | None = typer.Option(
        None, min=1, help="Keep one-minute candles only for the last N days before each close."
    ),
    skip_corpus: bool = typer.Option(False, "--skip-corpus", help="Do not rebuild the corpus."),
    skip_snapshot: bool = typer.Option(
        False, "--skip-snapshot", help="Do not rebuild the snapshot."
    ),
) -> None:
    """Rebuild the evidence corpus and the exchange snapshot for the backtest window."""
    from pmfa.config import load_config
    from pmfa.world.snapshot.build import window_start

    config = load_config()
    if series:
        config = _follow_only(config, series)
    start_day = _parse_day(start, "--start")
    end_day = _parse_day(end, "--end")
    start_at = _midnight(start_day) if start_day else window_start(config)
    now = datetime.now(UTC)
    end_at = min(now, datetime.combine(end_day, time.max, tzinfo=UTC)) if end_day else now
    if end_at <= start_at:
        raise typer.BadParameter("the window ends before it starts", param_hint="--end")
    if skip_corpus and skip_snapshot:
        typer.echo("nothing to do: both halves are skipped")
        return
    typer.echo(f"window {start_at:%Y-%m-%d} to {end_at:%Y-%m-%d %H:%M} UTC")
    if not skip_corpus:
        _build_corpus(config, start_at, end_at)
    if not skip_snapshot:
        _build_snapshot(config, start_at, end_at, minute_days)


def _build_corpus(config: Config, start: datetime, end: datetime) -> None:
    from pmfa.world.evidence.corpus import CorpusStore, build_corpus
    from pmfa.world.evidence.http import PoliteFetcher

    store = CorpusStore(config.paths.corpus_dir())
    typer.echo(f"corpus: fetching sources into {store.directory} (cached, spaced politely)")
    fetcher = PoliteFetcher.from_config(config)
    try:
        manifest = build_corpus(config, start, end, fetcher, store=store)
    finally:
        fetcher.close()
    counts = ", ".join(f"{k} {v}" for k, v in manifest.counts.items()) or "no documents"
    typer.echo(f"corpus: {sum(manifest.counts.values())} document(s) ({counts})")
    for line in manifest.failures:
        typer.echo(f"corpus: not fetched: {line}")


def _build_snapshot(
    config: Config, start: datetime, end: datetime, minute_days: int | None
) -> None:
    from pmfa.world.clocks import SystemClock
    from pmfa.world.kalshi.production import production_exchange
    from pmfa.world.snapshot.build import build_snapshot
    from pmfa.world.snapshot.store import SnapshotStore, snapshot_path

    path = snapshot_path(config)
    typer.echo(
        f"snapshot: building {path} at most {config.kalshi.max_requests_per_second:g} request(s)/s"
    )
    exchange = production_exchange(config, SystemClock())
    store = SnapshotStore(path)
    try:
        manifest = build_snapshot(
            config,
            exchange,
            store,
            start=start,
            end=end,
            minute_days=minute_days,
            progress=lambda line: typer.echo(f"snapshot: {line}"),
        )
    finally:
        store.close()
        client = getattr(exchange, "client", None)
        if client is not None:
            client.close()
    counts = ", ".join(f"{k} {v}" for k, v in manifest.counts.items())
    size_mb = path.stat().st_size / 1e6 if path.exists() else 0.0
    typer.echo(f"snapshot: {counts}; {size_mb:.1f} MB; {manifest.elapsed_seconds:.0f} s")


@data_app.command("status")
def status() -> None:
    """Print the corpus and snapshot manifests."""
    from pmfa.config import load_config
    from pmfa.world.evidence.corpus import CorpusStore
    from pmfa.world.evidence.corpus import manifest_json as corpus_json
    from pmfa.world.snapshot.store import SnapshotStore, manifest_json, snapshot_path

    config = load_config()
    corpus = CorpusStore(config.paths.corpus_dir())
    corpus_manifest = corpus.manifest()
    typer.echo(f"corpus ({corpus.directory}):")
    typer.echo(corpus_json(corpus_manifest) if corpus_manifest else "  none built")
    path = snapshot_path(config)
    typer.echo(f"snapshot ({path}):")
    if not path.exists():
        typer.echo("  none built")
        return
    store = SnapshotStore(path)
    try:
        manifest = store.manifest()
        if manifest is None:
            typer.echo(f"  no manifest (build incomplete); stored so far: {store.counts()}")
        else:
            typer.echo(manifest_json(manifest))
    finally:
        store.close()


def register(app: typer.Typer) -> None:
    app.add_typer(data_app, name="data")


__all__ = ["data_app", "register"]
