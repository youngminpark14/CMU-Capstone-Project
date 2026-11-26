"""Every decision record says where it came from, including when no checkpoint can be named."""

from __future__ import annotations

import re

import pytest

from .support import ROOT

DECISIONS = ROOT / "docs" / "decisions"
RECORDS = sorted(DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
FROM_SPEC_CHANGES = [p for p in RECORDS if int(p.name[:4]) <= 12]
"""Records 0001 to 0012: the spec's "Changes from the checkpoint documents"."""
NAMES_A_CHECKPOINT = re.compile(r"[Cc]heckpoints? \d\.\d")
SAYS_NONE_NAMED = "does not name the checkpoint"


INDEX_ROW = re.compile(r"^\| \[(\d{4})\]\(([^)]+)\) \| (.+?) \| [^|]+ \|$", re.M)


def origin(path) -> str:
    match = re.search(
        r"^## Origin\n(.*?)(?=^## |\Z)", path.read_text(encoding="utf-8"), re.M | re.S
    )
    assert match, f"{path.name} has no Origin section"
    return " ".join(match.group(1).split())


def test_the_twelve_spec_changes_are_all_present() -> None:
    assert len(FROM_SPEC_CHANGES) == 12


@pytest.mark.parametrize("path", FROM_SPEC_CHANGES, ids=lambda p: p.name[:4])
def test_each_spec_derived_record_names_its_checkpoint_or_says_none_is_named(path) -> None:
    text = origin(path)
    item = int(path.name[:4])
    assert f'"Changes from the checkpoint documents", item {item}' in text
    assert NAMES_A_CHECKPOINT.search(text) or SAYS_NONE_NAMED in text.lower(), text


@pytest.mark.parametrize("path", FROM_SPEC_CHANGES, ids=lambda p: p.name[:4])
def test_the_index_origin_column_matches_the_record(path) -> None:
    index = (DECISIONS / "README.md").read_text(encoding="utf-8")
    row = next(line for line in index.splitlines() if line.startswith(f"| [{path.name[:4]}]"))
    origin_cell = row.rstrip(" |").rsplit("|", 1)[1].strip()
    if NAMES_A_CHECKPOINT.search(origin(path)):
        assert "checkpoint" in origin_cell
    else:
        assert "checkpoint not named" in origin_cell


def test_the_index_lists_every_record_once_in_order_with_its_own_title() -> None:
    index = (DECISIONS / "README.md").read_text(encoding="utf-8")
    rows = INDEX_ROW.findall(index)
    assert [file for _, file, _ in rows] == [p.name for p in RECORDS]
    for number, file, title in rows:
        assert file.startswith(f"{number}-"), file
        first_line = (DECISIONS / file).read_text(encoding="utf-8").splitlines()[0]
        assert first_line == f"# {number}. {title}"
