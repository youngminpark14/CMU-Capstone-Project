"""Every test the guardrail table names exists where the table says it is.

docs/guardrails.md shows each guardrail by the tests that show it. A test named in a cell must be
a test function in a test file the same cell links, or, when the cell links no test file, in
tests/episodes/test_scenarios.py, as the sentence under the table says. A renamed or moved test
then fails here instead of leaving the page pointing at nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .support import ROOT, section

GUARDRAILS = ROOT / "docs" / "guardrails.md"
SCENARIOS = ROOT / "tests" / "episodes" / "test_scenarios.py"
TEST_LINK = re.compile(r"\]\((\.\./tests/[^)#\s]+\.py)\)")
TEST_NAME = re.compile(r"`(test_\w+)`")


def _shown_by_cells() -> list[tuple[str, str]]:
    """Each row's guardrail and its "Shown by" cell, from the table of every guardrail."""
    body = section(
        GUARDRAILS.read_text(encoding="utf-8"),
        "Every guardrail, where it is enforced, and its test",
    )
    rows = [line for line in body.splitlines() if line.startswith("| ")]
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    column = header.index("Shown by")
    cells = []
    for line in rows[1:]:
        if set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        cells.append((parts[0], parts[column]))
    return cells


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def _misplaced(cells: list[tuple[str, str]]) -> list[str]:
    """Each named test not found in the files its cell points at, with the row it is in."""
    problems = []
    for guardrail, cell in cells:
        linked = [(GUARDRAILS.parent / link).resolve() for link in TEST_LINK.findall(cell)]
        files = linked or [SCENARIOS]
        defined: set[str] = set()
        for path in files:
            if path.exists():
                defined |= _test_functions(path)
        for name in TEST_NAME.findall(cell):
            if name not in defined:
                where = ", ".join(str(p.relative_to(ROOT)) for p in files)
                problems.append(f"{name} (row '{guardrail}') is not in {where}")
    return problems


def test_the_table_names_tests() -> None:
    cells = _shown_by_cells()
    assert len(cells) > 30
    assert sum(len(TEST_NAME.findall(cell)) for _, cell in cells) > 50


def test_every_test_the_table_names_is_in_a_file_its_cell_links() -> None:
    problems = _misplaced(_shown_by_cells())
    assert not problems, "\n".join(problems)


def test_the_check_catches_a_test_in_the_wrong_file() -> None:
    gate_test = "`test_each_check_fails_alone`"
    assert _misplaced([("unlinked", gate_test)])
    gate_file = "[test_gate.py](../tests/decision/test_gate.py)"
    assert not _misplaced([("linked", f"{gate_test} in {gate_file}")])
    assert _misplaced([("renamed", f"`test_no_such_test_exists` in {gate_file}")])
