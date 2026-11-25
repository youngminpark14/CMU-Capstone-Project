"""``pmfa``: the single command-line entry point."""

from __future__ import annotations

import typer

from pmfa import __version__

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
