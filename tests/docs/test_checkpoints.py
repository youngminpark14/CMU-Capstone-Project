"""docs/checkpoints.md points only at things that exist, and names no person.

The page traces each capstone checkpoint to a file and a test, so a reader must be able to open
every one it names: each link, each repository path in code, each test function in the file it is
cited with, and each decision record by number. The checkpoint documents carry the names of their
author and of the instructors; the page must not. Those names are held here as hashes, so this
file does not write them into the repository either.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from .support import ROOT, without_code
from .test_links import LINK, SCHEME

PAGE = ROOT / "docs" / "checkpoints.md"
DECISIONS = ROOT / "docs" / "decisions"
CHECKPOINTS = ["1.1", "2.1", "3.1", "4.1", "5.1", "6.1"]

RECENCY_EXAMPLE = {
    "architecture.md#worked-example-recency-against-similarity": "docs/architecture.md",
    "scripts/recency_example.py": "scripts/recency_example.py",
    "tests/knowledge/test_recency_example.py": "tests/knowledge/test_recency_example.py",
}
"""The worked example for the feedback on checkpoint 3.1, written in a separate change."""

NAME_HASHES = {
    "fda5533a9bf821d4",
    "4d3971de62245c94",
    "92147289f239864f",
    "d549a5f531a62db6",
    "d5b191e600ae8462",
}
"""First 16 hex digits of the SHA-256 of each lower-case given name and surname in the documents."""

REPO_PATH = re.compile(r"`((?:src|tests|scripts|docs|examples|evaluation)/[\w./-]+)`")
TEST_NAME = re.compile(r"`(test_\w+)`")
RECORD_NUMBER = re.compile(r"\b(0\d{3})\b")


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


def _relative_links(text: str) -> list[str]:
    links = LINK.findall(without_code(text))
    return [link for link in links if not SCHEME.match(link) and not link.startswith("#")]


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = re.sub(r"^```.*?^```", "", path.read_text(encoding="utf-8"), flags=re.M | re.S)
    return {_slug(m) for m in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)}


def _resolves(link: str) -> bool:
    file_part, _, anchor = link.partition("#")
    target = (PAGE.parent / file_part).resolve() if file_part else PAGE
    if not target.exists():
        return False
    return not (anchor and target.suffix == ".md" and anchor not in _anchors(target))


def _cells(text: str) -> list[str]:
    """Table cells one by one, and every other line whole: a test is cited with its file there."""
    cells: list[str] = []
    for line in text.splitlines():
        if line.startswith("|"):
            cells.extend(line.strip("|").split(" | "))
        else:
            cells.append(line)
    return cells


def test_the_page_has_a_section_for_each_of_the_six_checkpoints() -> None:
    headings = re.findall(r"^## (.+)$", _text(), re.MULTILINE)
    for number in CHECKPOINTS:
        assert any(h.startswith(f"Checkpoint {number}:") for h in headings), number


def test_every_relative_link_resolves_to_a_file_and_a_heading() -> None:
    every = [link for link in LINK.findall(without_code(_text())) if not SCHEME.match(link)]
    links = [link for link in every if link not in RECENCY_EXAMPLE]
    assert links
    missing = [link for link in links if not _resolves(link)]
    assert not missing, f"docs/checkpoints.md links to missing targets: {missing}"


def test_every_repository_path_named_in_code_exists() -> None:
    paths = [p for p in REPO_PATH.findall(_text()) if p not in RECENCY_EXAMPLE]
    missing = [p for p in paths if not (ROOT / p).exists()]
    assert not missing, f"docs/checkpoints.md names missing paths: {missing}"


@pytest.mark.xfail(
    not any((ROOT / path).exists() for path in RECENCY_EXAMPLE.values() if path.endswith(".py")),
    reason="the recency worked example is added in a separate change",
    strict=True,
)
def test_the_recency_worked_example_it_points_to_exists() -> None:
    text = _text()
    for target in RECENCY_EXAMPLE:
        assert target in text, target
    absent = [t for t in RECENCY_EXAMPLE if not _resolves(t) and not (ROOT / t).exists()]
    assert not absent, f"not found: {absent}"


def test_every_test_it_cites_is_defined_in_the_file_cited_with_it() -> None:
    problems = []
    for cell in _cells(_text()):
        names = TEST_NAME.findall(cell)
        if not names:
            continue
        files = [
            (PAGE.parent / link).resolve()
            for link in _relative_links(cell)
            if link.endswith(".py") and "/tests/" in f"/{link}"
        ]
        sources = [f.read_text(encoding="utf-8") for f in files if f.exists()]
        if not sources:
            problems.append(f"{names} cited with no test file")
            continue
        for name in names:
            if not any(re.search(rf"^def {name}\(", s, re.MULTILINE) for s in sources):
                problems.append(f"{name} is not defined in {[f.name for f in files]}")
    assert not problems, problems


def test_every_decision_record_it_cites_exists() -> None:
    numbers = set(RECORD_NUMBER.findall(_text()))
    assert numbers
    existing = {p.name[:4] for p in DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md")}
    assert not numbers - existing, sorted(numbers - existing)


def test_the_page_names_no_author_or_instructor() -> None:
    words = set(re.findall(r"[a-z]+", _text().lower()))
    found = [w for w in words if hashlib.sha256(w.encode()).hexdigest()[:16] in NAME_HASHES]
    assert not found, "docs/checkpoints.md contains a person's name"


def test_the_name_check_recognises_a_name() -> None:
    probe = "".join(["dee", "pti"])
    assert hashlib.sha256(probe.encode()).hexdigest()[:16] in NAME_HASHES
