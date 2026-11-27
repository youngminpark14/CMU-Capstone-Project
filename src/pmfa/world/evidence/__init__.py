"""Evidence sources: Federal Reserve, BLS, generic feeds, the release calendar, frozen and live corpora.

Public entry points (import from the submodules; see each docstring):

- ``http.PoliteFetcher``: spaced, cached, generic-User-Agent fetching;
- ``fed.FedSource``: FOMC statements, minutes and speeches;
- ``bls.BlsSource`` and ``bls.import_manual``: CPI and Employment Situation releases;
- ``feed.FeedSource``: generic RSS or Atom feeds, secondary evidence, none by default;
- ``calendar.build_calendar``: FOMC decision days and BLS release dates;
- ``corpus.CorpusStore``, ``corpus.FrozenEvidence``, ``corpus.build_corpus``;
- ``live.LiveEvidence``: the Evidence port over live feeds.
"""
