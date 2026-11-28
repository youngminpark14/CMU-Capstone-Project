"""Every relative link in the README and the docs points at something that exists, and every
anchor in the pages a reader starts from names a heading."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .support import ARCHITECTURE, EVALUATION, README, REVIEW, ROOT, linked_documents, without_code

LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _relative_links(path: Path) -> list[str]:
    links = LINK.findall(without_code(path.read_text(encoding="utf-8")))
    return [link for link in links if not SCHEME.match(link) and not link.startswith("#")]


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = re.sub(r"^```.*?^```", "", path.read_text(encoding="utf-8"), flags=re.M | re.S)
    return {_slug(m) for m in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)}


@pytest.mark.parametrize("doc", linked_documents(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_resolves(doc: Path) -> None:
    missing = []
    for link in _relative_links(doc):
        target = (doc.parent / link.split("#", 1)[0]).resolve()
        if not target.exists():
            missing.append(link)
    assert not missing, f"{doc.relative_to(ROOT)} links to missing paths: {missing}"


@pytest.mark.parametrize(
    "doc",
    [README, ARCHITECTURE, EVALUATION, REVIEW],
    ids=["README.md", "docs/architecture.md", "docs/evaluation.md", "docs/review.md"],
)
def test_anchors_in_the_front_door_documents_exist(doc: Path) -> None:
    text = without_code(doc.read_text(encoding="utf-8"))
    broken = []
    for link in LINK.findall(text):
        if SCHEME.match(link) or "#" not in link:
            continue
        file_part, anchor = link.split("#", 1)
        target = (doc.parent / file_part).resolve() if file_part else doc
        if target.suffix == ".md" and anchor not in _anchors(target):
            broken.append(link)
    assert not broken, f"{doc.name} links to missing headings: {broken}"
