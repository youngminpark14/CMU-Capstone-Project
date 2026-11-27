"""Every decision record says where it came from, and the index agrees with it.

Records 0001 to 0012 are the spec's "Changes from the checkpoint documents": each must name the
checkpoints it changes and its spec item. Records 0013 onward were made while building; their
Origin starts with the build and may add one sentence naming the checkpoints they implement,
change or answer. The index's Origin column must name the same checkpoints as the record's Origin
section.
"""

from __future__ import annotations

import re

import pytest

from .support import ROOT

DECISIONS = ROOT / "docs" / "decisions"
INDEX = DECISIONS / "README.md"
RECORDS = sorted(DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
FROM_SPEC_CHANGES = [p for p in RECORDS if int(p.name[:4]) <= 12]
"""Records 0001 to 0012: the spec's "Changes from the checkpoint documents"."""
FROM_BUILD = [p for p in RECORDS if int(p.name[:4]) > 12]
CHECKPOINTS = {"1.1", "2.1", "3.1", "4.1", "5.1", "6.1"}
CHECKPOINT_MENTION = re.compile(r"[Cc]heckpoints? (\d\.\d(?:(?:,| and|, and) \d\.\d)*)")
""""checkpoint 4.1", "checkpoints 4.1 and 5.1", "checkpoints 2.1, 3.1, 5.1 and 6.1"."""
SAYS_UNAVAILABLE = re.compile(
    r"not available to this project|checkpoint documents are not available"
    r"|does not name the checkpoint|checkpoint not named",
    re.IGNORECASE,
)


INDEX_ROW = re.compile(r"^\| \[(\d{4})\]\(([^)]+)\) \| (.+?) \| [^|]+ \|$", re.M)


def origin(path) -> str:
    match = re.search(
        r"^## Origin\n(.*?)(?=^## |\Z)", path.read_text(encoding="utf-8"), re.M | re.S
    )
    assert match, f"{path.name} has no Origin section"
    return " ".join(match.group(1).split())


def checkpoints_named(text: str) -> set[str]:
    """The checkpoint numbers ``text`` names, written as "checkpoint(s) N.N[, N.N][ and N.N]"."""
    return {n for group in CHECKPOINT_MENTION.findall(text) for n in re.findall(r"\d\.\d", group)}


def index_cell(path) -> str:
    index = INDEX.read_text(encoding="utf-8")
    row = next(line for line in index.splitlines() if line.startswith(f"| [{path.name[:4]}]"))
    return row.rstrip(" |").rsplit("|", 1)[1].strip()


def test_the_twelve_spec_changes_are_all_present() -> None:
    assert len(FROM_SPEC_CHANGES) == 12


@pytest.mark.parametrize("path", FROM_SPEC_CHANGES, ids=lambda p: p.name[:4])
def test_each_spec_derived_record_names_its_checkpoints_and_its_spec_item(path) -> None:
    text = origin(path)
    item = int(path.name[:4])
    assert f'"Changes from the checkpoint documents", item {item}' in text
    assert checkpoints_named(text), f"{path.name} names no checkpoint: {text}"


@pytest.mark.parametrize("path", FROM_BUILD, ids=lambda p: p.name[:4])
def test_each_build_record_starts_its_origin_with_the_build(path) -> None:
    assert origin(path).startswith("Build, November 2026."), origin(path)


@pytest.mark.parametrize("path", [*RECORDS, INDEX], ids=lambda p: p.name[:4])
def test_nothing_says_the_checkpoint_documents_are_unavailable(path) -> None:
    text = " ".join(path.read_text(encoding="utf-8").split())
    assert not SAYS_UNAVAILABLE.search(text), SAYS_UNAVAILABLE.search(text).group(0)


@pytest.mark.parametrize("path", RECORDS, ids=lambda p: p.name[:4])
def test_only_real_checkpoints_are_named(path) -> None:
    assert checkpoints_named(origin(path)) <= CHECKPOINTS


@pytest.mark.parametrize("path", RECORDS, ids=lambda p: p.name[:4])
def test_the_index_origin_column_names_the_same_checkpoints_as_the_record(path) -> None:
    cell = index_cell(path)
    named = checkpoints_named(origin(path))
    assert set(re.findall(r"\d\.\d", cell)) == named, (cell, sorted(named))
    if path in FROM_SPEC_CHANGES:
        assert cell.endswith(f"; spec item {int(path.name[:4])}"), cell
    elif named:
        assert cell.startswith("build; checkpoint"), cell
    else:
        assert cell == "build", cell


def test_the_index_lists_every_record_once_in_order_with_its_own_title() -> None:
    index = INDEX.read_text(encoding="utf-8")
    rows = INDEX_ROW.findall(index)
    assert [file for _, file, _ in rows] == [p.name for p in RECORDS]
    for number, file, title in rows:
        assert file.startswith(f"{number}-"), file
        first_line = (DECISIONS / file).read_text(encoding="utf-8").splitlines()[0]
        assert first_line == f"# {number}. {title}"
