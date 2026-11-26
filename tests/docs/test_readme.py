"""The README's diagram, results and example paths agree with the repository."""

from __future__ import annotations

import json
import re

import pytest

from .support import README, ROOT, fenced_blocks, section

RUN_DIR = ROOT / "evaluation" / "runs" / "2026-11-26-market-baseline"
PAIRS = {"[": "]", "(": ")", "{": "}"}


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _bracket_problem(block: str) -> str | None:
    for number, line in enumerate(block.splitlines(), 1):
        if line.count('"') % 2:
            return f"line {number}: unbalanced quotes: {line.strip()}"
    stack: list[str] = []
    for char in re.sub(r'"[^"]*"', '""', block):
        if char in PAIRS:
            stack.append(PAIRS[char])
        elif char in PAIRS.values() and (not stack or stack.pop() != char):
            return f"unexpected {char!r}"
    return f"unclosed {stack}" if stack else None


def test_the_readme_has_a_mermaid_diagram_with_balanced_brackets_and_quotes() -> None:
    blocks = fenced_blocks(_readme(), "mermaid")
    assert blocks, "the README has no Mermaid diagram"
    for block in blocks:
        assert _bracket_problem(block) is None, _bracket_problem(block)


def test_the_bracket_check_catches_an_unbalanced_block() -> None:
    assert _bracket_problem('a["one"] --> b["two"') is not None
    assert _bracket_problem('a["one] --> b') is not None
    assert _bracket_problem('a["(x"] --> b[("store")]') is None


def _results_rows() -> list[list[str]]:
    body = section(_readme(), "Results")
    table = re.search(
        r"<!-- results-table:start.*?-->\n(.*?)<!-- results-table:end -->", body, re.S
    )
    assert table, "the Results section has no marked results table"
    lines = [line for line in table.group(1).strip().splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in lines[2:]]


def test_the_results_table_equals_metrics_json() -> None:
    metrics = json.loads((RUN_DIR / "metrics.json").read_text(encoding="utf-8"))
    by_horizon = metrics["systems"]["market"]["by_horizon"]
    rows = _results_rows()
    assert [r[0] for r in rows] == ["all", "7d", "3d", "1d"]
    for horizon, rows_n, markets, events, brier, interval in rows:
        block = by_horizon[horizon.removesuffix("d")]["answered"]
        assert int(rows_n) == block["sample"]["n_rows"]
        assert int(markets) == block["sample"]["n_markets"]
        assert int(events) == block["sample"]["n_events"]
        assert brier == f"{block['brier']['estimate']:.4f}"
        assert interval == f"{block['brier']['lower']:.4f} to {block['brier']['upper']:.4f}"


def test_the_calibration_error_quoted_in_results_equals_metrics_json() -> None:
    metrics = json.loads((RUN_DIR / "metrics.json").read_text(encoding="utf-8"))
    quoted = re.search(
        r"expected calibration error\s+over all horizons is (\d\.\d{4})",
        section(_readme(), "Results"),
    )
    assert quoted
    assert quoted.group(1) == f"{metrics['reliability']['market']['answered']['ece']:.4f}"


def _named_examples() -> list[str]:
    return sorted(set(re.findall(r"examples/episodes/([a-z0-9-]+)", _readme())))


def test_the_readme_names_all_five_examples() -> None:
    assert _named_examples() == [
        "cpi-ladder",
        "fed-minutes-hawkish",
        "phantom-edge-no-trade",
        "review-resize",
        "single-origin-syndicated",
    ]


@pytest.mark.parametrize("name", _named_examples())
def test_each_named_example_exists_with_inputs_and_outputs(name: str) -> None:
    folder = ROOT / "examples" / "episodes" / name
    for part in ("README.md", "world.json", "cassette.json", "expected/episode.json"):
        assert (folder / part).is_file(), f"{folder.relative_to(ROOT)}/{part} is missing"


def test_the_exits_the_readme_states_match_the_expected_episodes() -> None:
    claims = re.findall(r"examples/episodes/([a-z0-9-]+)/\).*?exit `(\w+)`", _readme())
    assert claims
    for name, exit_name in claims:
        episode = json.loads(
            (ROOT / "examples" / "episodes" / name / "expected" / "episode.json").read_text(
                encoding="utf-8"
            )
        )
        assert episode["exit"] == exit_name, name
