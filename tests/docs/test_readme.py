"""The README and the pages its details moved to agree with the repository: the architecture
diagrams, the results and what is said about them, the decision-record counts, and the examples."""

from __future__ import annotations

import json
import re

import pytest

from .support import ARCHITECTURE, EVALUATION, README, REVIEW, ROOT, fenced_blocks, section

RUN_DIR = ROOT / "evaluation" / "runs" / "2026-11-26-market-baseline"
PAIRS = {"[": "]", "(": ")", "{": "}"}
BASELINE = "The market baseline (2026-11-26)"
"""The subsection of docs/evaluation.md that discusses the published run's numbers."""


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _baseline() -> str:
    """The evaluation page's discussion of the published run, where the README's detail moved."""
    return section(EVALUATION.read_text(encoding="utf-8"), BASELINE, level=3)


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


def test_the_architecture_diagrams_have_balanced_brackets_and_quotes() -> None:
    blocks = fenced_blocks(ARCHITECTURE.read_text(encoding="utf-8"), "mermaid")
    assert blocks, "docs/architecture.md has no Mermaid diagram"
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


def test_the_calibration_error_quoted_for_the_published_run_equals_metrics_json() -> None:
    metrics = json.loads((RUN_DIR / "metrics.json").read_text(encoding="utf-8"))
    quoted = re.search(
        r"expected calibration error\s+over all horizons is (\d\.\d{4})", _baseline()
    )
    assert quoted
    assert quoted.group(1) == f"{metrics['reliability']['market']['answered']['ece']:.4f}"


BAND_WORDING = {
    "below 0.10": "priced below 0.10",
    "0.10 to 0.90": "from 0.10 to 0.90",
    "above 0.90": "above 0.90",
}


NUMBER_WORDS = {"Four": 4, "Five": 5, "Six": 6, "Seven": 7, "Eight": 8, "Nine": 9}


def _percent(share: float, quoted: str) -> str:
    """A share as a percentage, at the number of decimals the page quotes it with."""
    return f"{share * 100:.{len(quoted.partition('.')[2])}f}"


def test_the_error_shares_quoted_for_the_published_run_equal_metrics_json() -> None:
    """Each share is checked where the page attaches it: to its band, event or market."""
    metrics = json.loads((RUN_DIR / "metrics.json").read_text(encoding="utf-8"))
    found = metrics["systems"]["market"]["error_concentration"]
    results = " ".join(_baseline().split())
    assert f"over the {found['n_rows']} rows, {found['squared_error']:.4f}" in results
    checked: list[str] = []
    for band in found["bands"]:
        phrase = BAND_WORDING[band["name"]]
        quoted = re.search(rf"(\d+) rows {re.escape(phrase)} carry (\d+\.\d+)%", results)
        assert quoted, f"Results quotes no share for the band {band['name']}"
        assert int(quoted.group(1)) == band["n_rows"], band["name"]
        assert quoted.group(2) == _percent(band["share"], quoted.group(2)), band["name"]
        checked.append(quoted.group(2))
    event = found["top_event"]
    quoted = re.search(
        rf"{re.escape(event['name'])}, carries (\d+\.\d+)% of the error \((\d+) rows\)", results
    )
    assert quoted, f"Results quotes no share for {event['name']}"
    assert quoted.group(1) == _percent(event["share"], quoted.group(1))
    assert int(quoted.group(2)) == event["n_rows"]
    checked.append(quoted.group(1))
    top_two = found["top_markets"][:2]
    for market in found["top_markets"]:
        suffix = market["name"].removeprefix(event["name"] + "-")
        quoted = re.search(rf"(\d+\.\d+)% \({re.escape(suffix)}\)", results)
        assert quoted, f"Results quotes no share for {market['name']}"
        assert quoted.group(1) == _percent(market["share"], quoted.group(1)), market["name"]
        checked.append(quoted.group(1))
    above = next(band for band in found["bands"] if band["name"] == "above 0.90")
    quoted = re.search(rf"(\w+) of those {above['n_rows']} rows carry (\d+\.\d+)%", results)
    assert quoted, "Results quotes no share for the two largest markets together"
    assert NUMBER_WORDS.get(quoted.group(1)) == sum(m["n_rows"] for m in top_two)
    assert quoted.group(2) == _percent(sum(m["share"] for m in top_two), quoted.group(2))
    checked.append(quoted.group(2))
    assert sorted(re.findall(r"(\d+\.\d+)%", results)) == sorted(checked), (
        "Results quotes a percentage that is not tied to a band, event or market"
    )


def test_the_docs_say_the_canary_guarded_the_published_run_only_if_an_index_was_built() -> None:
    manifest = json.loads((RUN_DIR / "manifest.json").read_text(encoding="utf-8"))
    checks = manifest["leakage_checks"]
    results = " ".join(_baseline().split())
    assert "canary" in results
    claims_guarded = re.search(
        r"canary (?:(?:was|were|is|has been|had been|has|had) )?"
        r"(?:guarded|checked|exercised|aborted|tripped|caught)\b(?! nothing)",
        results,
    )
    if checks["index_built"] and checks["retrievals_checked"] > 0:
        assert "built no retrieval index" not in results
        assert "canary guarded nothing" not in results
    else:
        assert "built no retrieval index" in results
        assert "canary guarded nothing" in results
        assert claims_guarded is None, f"Results says {claims_guarded.group(0)!r}"


DECISION_RECORDS = sorted((ROOT / "docs" / "decisions").glob("[0-9][0-9][0-9][0-9]-*.md"))


@pytest.mark.parametrize("doc", [README, REVIEW], ids=["README.md", "docs/review.md"])
def test_every_count_of_decision_records_equals_the_record_files(doc) -> None:
    text = " ".join(doc.read_text(encoding="utf-8").split())
    stated = re.findall(r"(\d+) (?:short |decision )*records\b", text)
    assert stated, f"{doc.name} states no number of decision records"
    for number in stated:
        assert int(number) == len(DECISION_RECORDS), (
            f"{doc.name} says {number} records; docs/decisions holds {len(DECISION_RECORDS)}"
        )


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
