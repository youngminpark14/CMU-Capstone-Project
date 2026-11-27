"""Shared helpers for the document checks: where the documents are and how to read their parts."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
EVALUATION = ROOT / "docs" / "evaluation.md"
REVIEW = ROOT / "docs" / "review.md"

FENCE = re.compile(r"^```([\w-]*)[^\n]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


def linked_documents() -> list[Path]:
    """Every Markdown file whose relative links must resolve."""
    return [
        README,
        *sorted((ROOT / "docs").glob("*.md")),
        *sorted((ROOT / "docs" / "decisions").glob("*.md")),
    ]


def fenced_blocks(text: str, language: str | None = None) -> list[str]:
    """The bodies of the fenced code blocks in ``text``, optionally only one language."""
    return [body for lang, body in FENCE.findall(text) if language is None or lang == language]


def without_code(text: str) -> str:
    """``text`` with fenced blocks and inline code spans removed, so only prose links remain."""
    text = FENCE.sub("", text)
    return re.sub(r"`[^`\n]*`", "", text)


def section(text: str, heading: str, level: int = 2) -> str:
    """The body of the section named ``heading`` (a level-2 heading by default), up to the next
    heading of its level or above. A page's one level-1 heading is its title, so it ends nothing."""
    hashes = "#" * level
    match = re.search(
        rf"^{hashes} {re.escape(heading)}\n(.*?)(?=^#{{2,{level}}} |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"no '{hashes} {heading}' section"
    return match.group(1)
