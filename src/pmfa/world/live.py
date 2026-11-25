"""World builders for the live modes: shadow (live data, simulated fills) and demo (Kalshi's demo exchange).

What sits behind each port, per the spec's modes table:

    port        shadow                                   demo
    exchange    production market data, read-only        Kalshi demo exchange
    executor    paper fills against the live book        DemoExecutor: real orders, mock funds
    evidence    live official feeds over the corpus      the same
    knowledge   persistent Chroma under the index dir    the same
    models      gateway: live, seconds cap, cassettes    the same
    review      fill_tagged (pause with ``review``)      pause, always

Safety properties, each checked by a test:

- **Shadow cannot trade.** Its exchange is ``ProductionExchange``: no order
  methods, an unsigned client, and no credential is read to build it. Orders go
  to the paper executor, which only writes to the state store.
- **Demo never falls back to production.** It needs the demo credentials (the
  variables named in ``config.kalshi``) and the demo host; without either it
  refuses to build, naming what is missing. Every order needs a human.
- **No tracing service.** Both builders switch LangSmith and LangChain tracing
  off for the process, so an inherited environment variable cannot send prompts
  anywhere. No builder logs or prints a secret.

The two modes keep separate state files (``live_paths``): paper fills and the
demo subaccount's real fills never share one portfolio, and a halt flag or a
review in one mode's database applies to that mode only. ``pmfa halt`` without
``--mode`` therefore sets the flag in both databases.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pmfa.errors import ConfigError
from pmfa.ports import World
from pmfa.records.base import RunMode
from pmfa.records.decision import ReviewPolicy

if TYPE_CHECKING:
    from pmfa.config import Config
    from pmfa.knowledge.retriever import ChromaKnowledge
    from pmfa.ports import Clock, Models
    from pmfa.state.store import SqliteStore
    from pmfa.world.evidence.live import LiveEvidence

TRACING_VARIABLES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)
"""The switches LangSmith and LangChain read to decide whether to send traces."""


def disable_tracing(environ: MutableMapping[str, str] | None = None) -> None:
    """Switch LangSmith / LangChain tracing off for this process, whatever was inherited."""
    env = os.environ if environ is None else environ
    for name in TRACING_VARIABLES:
        env[name] = "false"


def live_paths(config: Config, mode: RunMode) -> tuple[Path, Path]:
    """The state database and the checkpoint database of a live mode.

    Shadow uses ``config.paths.state_db()`` and ``checkpoint_db()``. Demo keeps
    its own pair next to them (``demo-state.sqlite``, ``demo-checkpoints.sqlite``),
    so the demo subaccount's fills never enter the shadow paper account.
    """
    mode = RunMode(mode)
    if mode is RunMode.SHADOW:
        return config.paths.state_db(), config.paths.checkpoint_db()
    if mode is RunMode.DEMO:
        data = Path(config.paths.data_dir)
        return data / "demo-state.sqlite", data / "demo-checkpoints.sqlite"
    raise ValueError(f"{mode.value} is not a live mode")


def cassette_dir(config: Config, mode: RunMode) -> Path:
    """Where the gateway records a live mode's model calls."""
    return Path(config.paths.data_dir) / "cassettes" / RunMode(mode).value


@dataclass(frozen=True)
class LiveWorld(World):
    """A live ``World`` that owns open resources (database, index, HTTP clients). Close it."""

    state_db: Path
    checkpoint_db: Path
    closers: tuple[Callable[[], None], ...] = ()

    def close(self) -> None:
        """Close what the builder opened, newest first. A failing close does not stop the rest."""
        for close in reversed(self.closers):
            with contextlib.suppress(Exception):
                close()


def live_models(config: Config, mode: RunMode) -> Models:
    """The gateway for a live mode: per-episode seconds cap on, cassettes recorded, no cache.

    The Anthropic key is read from the environment when the first call is made,
    not here, so building a world needs no key.
    """
    from pmfa.models.cassette import CassetteRecorder
    from pmfa.models.gateway import Gateway

    return Gateway(
        config, recorder=CassetteRecorder(cassette_dir(config, mode)), enforce_seconds=True
    )


def _common(
    config: Config, mode: RunMode, clock: Clock, stack: contextlib.ExitStack
) -> tuple[SqliteStore, LiveEvidence, ChromaKnowledge]:
    """The store, evidence and knowledge ports both live modes share, registered for closing."""
    from pmfa.knowledge.retriever import ChromaKnowledge
    from pmfa.state.store import SqliteStore
    from pmfa.world.evidence.corpus import CorpusStore
    from pmfa.world.evidence.http import PoliteFetcher
    from pmfa.world.evidence.live import LiveEvidence

    state_db, _ = live_paths(config, mode)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(state_db)
    stack.callback(store.close)
    fetcher = PoliteFetcher.from_config(config)
    stack.callback(fetcher.close)
    evidence = LiveEvidence(config, fetcher, CorpusStore(config.paths.corpus_dir()), clock=clock)
    knowledge = ChromaKnowledge(config, path=config.paths.index_dir())
    stack.callback(knowledge.close)
    return store, evidence, knowledge


def build_shadow_world(
    config: Config, *, models: Models | None = None, review: bool = False
) -> LiveWorld:
    """Shadow mode: live production prices, read-only; paper fills against the live book.

    ``review`` pauses approve-tier proposals for a human (``ReviewPolicy.PAUSE``);
    without it they are filled on paper and tagged (``FILL_TAGGED``). ``models``
    replaces the live gateway (tests and plumbing checks).
    """
    from pmfa.execution.paper import PaperExecutor
    from pmfa.world.clocks import SystemClock
    from pmfa.world.kalshi.production import production_exchange

    disable_tracing()
    mode = RunMode.SHADOW
    clock = SystemClock()
    with contextlib.ExitStack() as stack:
        exchange = production_exchange(config, clock)
        stack.callback(exchange.client.close)
        store, evidence, knowledge = _common(config, mode, clock, stack)
        executor = PaperExecutor(
            store,
            clock,
            config,
            exchange,
            exchange.get_order_book,
        )
        state_db, checkpoint_db = live_paths(config, mode)
        world = LiveWorld(
            mode=mode,
            config=config,
            clock=clock,
            exchange=exchange,
            executor=executor,
            evidence=evidence,
            knowledge=knowledge,
            models=models if models is not None else live_models(config, mode),
            store=store,
            review_policy=ReviewPolicy.PAUSE if review else ReviewPolicy.FILL_TAGGED,
            state_db=state_db,
            checkpoint_db=checkpoint_db,
            closers=(stack.pop_all().close,),
        )
    return world


def build_demo_world(config: Config, *, models: Models | None = None) -> LiveWorld:
    """Demo mode: Kalshi's demo exchange for data and orders, mock funds, a human approves every order.

    Raises ``ConfigError`` when ``config.kalshi.demo_base_url`` is not a demo
    host, or when the demo credentials are not set (the message names the
    variables, never a value). There is no fallback to any other host.
    """
    from pmfa.world.clocks import SystemClock
    from pmfa.world.kalshi.demo import DEMO_HOSTS, demo_executor, is_demo_url

    disable_tracing()
    cfg = config.kalshi
    if not is_demo_url(cfg.demo_base_url):
        raise ConfigError(
            "kalshi.demo_base_url must be a Kalshi demo host "
            f"({', '.join(sorted(DEMO_HOSTS))}); refusing {cfg.demo_base_url!r}"
        )
    mode = RunMode.DEMO
    clock = SystemClock()
    try:
        executor = demo_executor(config, clock)
    except ConfigError as exc:
        # The inner message names the variables itself when none is set; a partial or
        # unreadable credential gets its own detail, appended once.
        names = (cfg.demo_key_id_env, cfg.demo_private_key_env, cfg.demo_private_key_path_env)
        detail = "" if all(name in str(exc) for name in names) else f" ({exc})"
        raise ConfigError(
            f"demo mode needs the demo API credentials in the environment: {cfg.demo_key_id_env}, "
            f"and {cfg.demo_private_key_env} or {cfg.demo_private_key_path_env}{detail}"
        ) from None
    with contextlib.ExitStack() as stack:
        stack.callback(executor.client.close)
        stack.callback(executor.data.client.close)
        store, evidence, knowledge = _common(config, mode, clock, stack)
        state_db, checkpoint_db = live_paths(config, mode)
        world = LiveWorld(
            mode=mode,
            config=config,
            clock=clock,
            exchange=executor.data,
            executor=executor,
            evidence=evidence,
            knowledge=knowledge,
            models=models if models is not None else live_models(config, mode),
            store=store,
            review_policy=ReviewPolicy.PAUSE,
            state_db=state_db,
            checkpoint_db=checkpoint_db,
            closers=(stack.pop_all().close,),
        )
    return world


def build_live_world(
    mode: RunMode, config: Config, *, models: Models | None = None, review: bool = False
) -> LiveWorld:
    """The world of a live mode. ``review`` only matters for shadow (demo always pauses)."""
    mode = RunMode(mode)
    if mode is RunMode.SHADOW:
        return build_shadow_world(config, models=models, review=review)
    if mode is RunMode.DEMO:
        return build_demo_world(config, models=models)
    raise ValueError(f"{mode.value} is not a live mode")


__all__ = [
    "TRACING_VARIABLES",
    "LiveWorld",
    "build_demo_world",
    "build_live_world",
    "build_shadow_world",
    "cassette_dir",
    "disable_tracing",
    "live_models",
    "live_paths",
]
