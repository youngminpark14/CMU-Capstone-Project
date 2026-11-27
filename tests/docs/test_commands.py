"""Every ``pmfa`` command shown in the README, the architecture page and the evaluation page
parses against the real CLI.

A command line is checked word by word against the command tree that Typer builds:
each subcommand must exist under its parent, and each ``--option`` must be an
option of the command it is given to. Words in capitals such as ``NAME`` are
placeholders and are not checked.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import typer.core
import typer.main

from pmfa.cli.app import app

from .support import ARCHITECTURE, EVALUATION, README, fenced_blocks

PREFIX = re.compile(r"^(?:uv run (?:--no-sync )?)?pmfa(?:\s|$)")


def _commands(path: Path) -> list[str]:
    """``pmfa`` lines from bash blocks and inline code spans, without the ``uv run`` prefix."""
    text = path.read_text(encoding="utf-8")
    lines = [
        line.strip()
        for block in fenced_blocks(text, "bash")
        for line in block.splitlines()
        if PREFIX.match(line.strip())
    ]
    lines += [
        span.strip() for span in re.findall(r"`([^`\n]+)`", text) if PREFIX.match(span.strip())
    ]
    return [re.sub(r"^uv run (?:--no-sync )?", "", line).split("  #")[0].strip() for line in lines]


def _problems(line: str, root: typer.core.TyperCommand | typer.core.TyperGroup) -> list[str]:
    words = shlex.split(line)[1:]
    command = root
    i = 0
    while (
        i < len(words)
        and isinstance(command, typer.core.TyperGroup)
        and not words[i].startswith("-")
    ):
        if words[i].isupper():  # a placeholder such as COMMAND: nothing more to check
            return []
        sub = command.commands.get(words[i])
        if sub is None:
            return [f"{words[i]!r} is not a subcommand of {command.name!r}"]
        command, i = sub, i + 1
    options = {"--help"}
    for param in command.params:
        if isinstance(param, typer.core.TyperOption):
            options.update(param.opts)
            options.update(param.secondary_opts)
    return [
        f"{word.split('=', 1)[0]!r} is not an option of {command.name!r}"
        for word in words[i:]
        if word.startswith("--") and word.split("=", 1)[0] not in options
    ]


@pytest.mark.parametrize(
    "doc",
    [README, ARCHITECTURE, EVALUATION],
    ids=["README.md", "docs/architecture.md", "docs/evaluation.md"],
)
def test_every_pmfa_command_shown_parses(doc: Path) -> None:
    commands = _commands(doc)
    assert commands, f"no pmfa command found in {doc.name}"
    root = typer.main.get_command(app)
    problems = {line: p for line in commands if (p := _problems(line, root))}
    assert not problems, problems


def test_the_commands_table_names_every_subcommand() -> None:
    table = re.search(
        r"^## Commands\n(.*?)(?=^## |\Z)", ARCHITECTURE.read_text(encoding="utf-8"), re.M | re.S
    )
    assert table
    named = set(re.findall(r"^\| `pmfa ([\w-]+)", table.group(1), re.MULTILINE))
    root = typer.main.get_command(app)
    assert named == set(root.commands)  # type: ignore[attr-defined]


def test_the_checker_rejects_an_unknown_option_and_subcommand() -> None:
    root = typer.main.get_command(app)
    assert _problems("pmfa backtest --dry-run --no-such-option", root)
    assert _problems("pmfa review approve-all", root)
    assert not _problems("pmfa review resize ID --contracts 5 --mode demo", root)
