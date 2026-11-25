"""The frozen data snapshot that backtests read instead of the exchange.

One SQLite file holds the followed series, their events and markets (the final
records, with results), and hourly and one-minute candles. It is rebuilt by
``pmfa data build`` and never committed (``data/`` is ignored by git).

Public entry points (import from the submodules; see each docstring):

- ``store.SnapshotStore`` and ``store.snapshot_path``: the file and its manifest;
- ``build.build_snapshot``: fill a store from the exchange, resumably;
- ``reader.SnapshotExchange``: the ``Exchange`` port over a store, as of a clock;
- ``book.execution_book``: the book a backtest order meets, for the paper executor.
"""
