"""Serve the state database over MCP on stdio: ``python -m pmfa.state --db PATH``.

stdout carries protocol messages only; logs go to stderr. The server started
this way has no Knowledge or Exchange port, so it serves the stored state
(episodes, traces, nodes, beams, rejections, ledger, calibration, halt, stage);
searches and rules report that their port is unavailable.
"""

from __future__ import annotations

import argparse
import logging
import sys

from pmfa.state import mcp_compat
from pmfa.state.server import build_server
from pmfa.state.service import StateService
from pmfa.state.store import SqliteStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pmfa.state", description="Serve the pmfa state database over MCP (stdio)."
    )
    parser.add_argument("--db", required=True, help="Path of the SQLite state database.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    store = SqliteStore(args.db)
    try:
        mcp_compat.run_stdio(build_server(StateService(store)))
    finally:
        store.close()


if __name__ == "__main__":
    main()
