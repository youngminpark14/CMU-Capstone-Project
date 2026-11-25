"""``pmfa``: the single command-line entry point.

Each group of subcommands lives in its own module under ``pmfa.cli`` and exposes
``register(app)``. Modules are imported when the command line starts, so a
subcommand's heavy dependencies load only when that subcommand runs.
"""

from __future__ import annotations

import importlib

import typer

from pmfa import __version__

COMMAND_MODULES = (
    "pmfa.cli.replay",  # replay, trace
    "pmfa.cli.data",  # snapshot and corpus
    "pmfa.cli.backtest",  # backtest, report
    "pmfa.cli.live",  # shadow, demo
    "pmfa.cli.oversight",  # review, halt, stage
    "pmfa.cli.state",  # state-server, resolve, config
)

app = typer.Typer(
    name="pmfa",
    help="Forecast Kalshi prediction markets and size positions on them. Educational project; mock funds only.",
    no_args_is_help=True,
    add_completion=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"pmfa {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Forecast Kalshi prediction markets and size positions on them."""


def _register_commands() -> None:
    for name in COMMAND_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:  # the module itself is absent; anything else is a real error
                continue
            raise
        module.register(app)


_register_commands()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
