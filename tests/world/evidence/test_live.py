"""``LiveEvidence.recheck``: a changed or withdrawn source behind an open position halts the live modes.

The stored copy of a document never changes, so the self-halt check has to ask
the source again. These tests use the recorded Federal Reserve pages behind a
fake fetcher whose minutes page is edited, then removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pmfa.config import Config, PathsConfig, SourcesConfig
from pmfa.errors import NotFound
from pmfa.execution.halt import enforce
from pmfa.records.decision import OrderStatus
from pmfa.records.market import Side
from pmfa.state.store import MEMORY, SqliteStore
from pmfa.world.clocks import SimClock
from pmfa.world.evidence.corpus import CorpusStore
from pmfa.world.evidence.http import Fetched, FetchError
from pmfa.world.evidence.live import LiveEvidence
from tests.execution.factories import episode, fill, order, paper

from .conftest import FED, FakeFetcher

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
MINUTES_ID = "fed:fomcminutes20261028"
MINUTES_URL = f"{FED}/monetarypolicy/fomcminutes20261028.htm"
SPEECH_ID = "fed:waller20261110a"
ORIGINAL = "A joint meeting of the Federal Open Market Committee"


class ChangingFetcher(FakeFetcher):
    """The recorded pages; ``status`` makes every page answer that HTTP error instead."""

    def __init__(self, pages: dict[str, str]) -> None:
        super().__init__(pages, refused=("bls.gov",))
        self.status: int | None = None

    def get(self, url: str, *, refresh: bool = False) -> Fetched:
        if self.status is not None and "/json/" not in url:
            self.calls.append((url, refresh))
            raise FetchError(url, self.status, f"{url} answered HTTP {self.status}")
        return super().get(url, refresh=refresh)


@pytest.fixture
def config(tmp_path):
    return Config(
        paths=PathsConfig(data_dir=str(tmp_path / "data")),
        sources=SourcesConfig(manual_dir=str(tmp_path / "manual")),
    )


@pytest.fixture
def setup(config, fed_pages, tmp_path):
    fetcher = ChangingFetcher(dict(fed_pages))
    clock = SimClock(NOW)
    live = LiveEvidence(config, fetcher, CorpusStore(tmp_path / "corpus"), clock=clock)
    live.poll(force=True)
    assert ORIGINAL in live.get_document(MINUTES_ID, as_of=NOW).text
    return live, fetcher, clock


def test_an_unchanged_page_rechecks_to_the_same_hash_without_touching_the_store(setup):
    live, fetcher, _ = setup
    for doc_id in (MINUTES_ID, SPEECH_ID):
        stored = live.get_document(doc_id, as_of=NOW)
        again = live.recheck(doc_id, as_of=NOW)
        assert again.content_hash == stored.content_hash and again.text == stored.text
        assert (stored.url, True) in fetcher.calls  # fetched from the site, not the cache


def test_a_changed_page_has_a_new_hash_and_the_stored_copy_stays(setup):
    live, fetcher, _ = setup
    stored = live.get_document(MINUTES_ID, as_of=NOW)
    fetcher.pages[MINUTES_URL] = fetcher.pages[MINUTES_URL].replace(ORIGINAL, "A revised meeting")
    current = live.recheck(MINUTES_ID, as_of=NOW)
    assert current.content_hash != stored.content_hash and "A revised meeting" in current.text
    assert live.get_document(MINUTES_ID, as_of=NOW) == stored  # history is not rewritten


def test_a_page_that_is_gone_is_withdrawn_and_a_server_error_is_not(setup):
    live, fetcher, clock = setup
    fetcher.status = 503
    assert live.recheck(MINUTES_ID, as_of=NOW) == live.get_document(MINUTES_ID, as_of=NOW)
    assert any("503" in f for f in live.last_failures)

    clock.advance(600)  # past the re-check interval
    fetcher.status = 404
    with pytest.raises(NotFound, match="gone"):
        live.recheck(MINUTES_ID, as_of=clock.now())


def test_rechecks_are_spaced_by_the_poll_interval(setup):
    live, fetcher, clock = setup
    live.recheck(MINUTES_ID, as_of=NOW)
    asked = len(fetcher.calls)
    clock.advance(10)
    live.recheck(MINUTES_ID, as_of=clock.now())
    assert len(fetcher.calls) == asked
    clock.advance(600)
    live.recheck(MINUTES_ID, as_of=clock.now())
    assert len(fetcher.calls) == asked + 1


def holding_store(doc_hash: str) -> SqliteStore:
    """An open position from an episode that cited the minutes, whose hash the store remembers."""
    store = SqliteStore(MEMORY)
    store.save_episode(episode("ep-1", passage_ids=[f"{MINUTES_ID}#0"]))
    f = fill(Side.YES, 10, "0.62", "0.17", NOW - timedelta(days=1), fill_id="f1")
    store.save_order(order("o1", OrderStatus.FILLED, fills=[f]))
    store.remember_document(MINUTES_ID, doc_hash, NOW - timedelta(days=1))
    return store


@pytest.mark.parametrize(
    ("change", "reason"),
    [("edit", "source changed"), ("remove", "source withdrawn")],
)
def test_enforce_halts_the_live_world_when_the_cited_source_changes_or_goes(
    setup, config, change, reason
):
    live, fetcher, clock = setup
    store = holding_store(live.get_document(MINUTES_ID, as_of=NOW).content_hash)
    executor, _, _ = paper()
    assert enforce(store, executor, config, clock, live) == []
    assert not store.get_halt().halted

    clock.advance(600)
    if change == "edit":
        page = fetcher.pages[MINUTES_URL]
        fetcher.pages[MINUTES_URL] = page.replace(ORIGINAL, "A revised meeting")
    else:
        del fetcher.pages[MINUTES_URL]
    found = enforce(store, executor, config, clock, live)
    assert [r.split(":")[0] for r in found] == [reason]
    assert store.get_halt().halted and store.get_halt().reason.startswith(reason)
