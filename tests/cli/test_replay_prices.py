"""``pmfa replay`` marks each market price that is a last trade rather than a YES mid.

The forecast table's price is the YES mid when the market's quote has two sides,
and the last trade when one side is empty. A reader comparing strikes needs to
know which is which, so the replay decides per row from the episode's recorded
quote and marks the last trades.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pmfa.cli.app import app

runner = CliRunner()
EPISODES = Path(__file__).resolve().parents[2] / "examples" / "episodes"


def price_cell(output: str, ticker: str) -> str:
    """``ticker``'s market price in the forecast table, with its mark appended when it has one."""
    rows = [line for line in output.splitlines() if line.startswith(ticker + " ")]
    assert len(rows) == 1, rows
    cells = rows[0].split()
    return cells[-2] + "*" if cells[-1] == "*" else cells[-1]


def test_a_last_trade_price_is_marked_and_a_two_sided_mid_is_not():
    out = runner.invoke(app, ["replay", str(EPISODES / "cpi-ladder")]).output
    # T3.3 is quoted 0.71 / 1.00: the ask side is empty, so 0.880 is the last trade.
    assert price_cell(out, "KXCPIYOY-26SEP-T3.3") == "0.880*"
    # T3.4 is quoted on both sides, so its price is the mid and carries no mark.
    assert price_cell(out, "KXCPIYOY-26SEP-T3.4") == "0.655"
    assert "or the last trade where one side of the quote is empty (marked *)" in " ".join(
        out.split()
    )


def test_a_one_sided_quote_at_the_floor_is_marked_too():
    out = runner.invoke(app, ["replay", str(EPISODES / "fed-minutes-hawkish")]).output
    # Bid 0.00 and ask 0.01: the bid side is empty.
    assert price_cell(out, "KXFEDDECISION-26OCT-C25") == "0.010*"
    assert price_cell(out, "KXFEDDECISION-26OCT-H0") == "0.825"
