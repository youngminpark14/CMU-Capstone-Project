"""The saved trace of the second of two episodes that share one instant in the scoring pass.

The setup is that of ``test_scoring_same_instant``: nine unfilled orders from an
earlier day, then two scheduled episodes for the Fed event at the same clock
time. The first buys the hold market, priced from the one-minute candle that
ends two minutes later. The second episode runs on a recording, as the backtest
runs every pipeline episode. Its trace must carry no price or outcome from after
its as-of time, and must still replay to the same decision.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pmfa.config import Config
from pmfa.evaluation.backtest import replay_check
from pmfa.examples import TapeModels, stored_card
from pmfa.state.store import SqliteStore
from pmfa.world.backtest import BacktestData, build_backtest_world
from pmfa.world.clocks import SimClock
from pmfa.world.recorded import RecordedRejection, RecordedWorldFile
from pmfa.world.recording import Recording, save_example
from tests.evaluation.backtest.support import DOC1_AT, FED, scripted
from tests.evaluation.backtest.test_scoring_same_instant import _trigger, _unfilled


def _second_trace(
    data_config: Config, tmp_path: Path, *, rejection: tuple[str, str] | None = None
) -> tuple[RecordedWorldFile, str, list[str]]:
    """Run both episodes, the second on a recording; save its trace and replay-check it.

    ``rejection`` is a critic rejection (claim, reason) stored for the Fed event by an
    earlier episode. Returns the second episode's file, the replay verdict, and the ids of
    the first episode's orders.
    """
    from pmfa.episode import EpisodeController

    data = BacktestData(data_config)
    clock = SimClock(DOC1_AT)
    store = SqliteStore(tmp_path / "scoring.sqlite")
    tape = TapeModels(scripted(data_config), data_config)
    world = build_backtest_world(
        data,
        data_config,
        clock=clock,
        store=store,
        models=tape,
        knowledge=data.guard(data.knowledge(tmp_path / "index")),
    )
    try:
        world.executor.mark(DOC1_AT - timedelta(days=1))  # type: ignore[attr-defined]
        for i in range(world.config.oversight.fill_failure_window - 1):
            store.save_order(_unfilled(i))
        if rejection is not None:
            store.record_rejection("earlier", FED, *rejection)

        with EpisodeController(world) as controller:
            first = controller.run(_trigger("a"))
        recording = Recording(world)
        second_trigger = _trigger("b")
        with EpisodeController(recording.world) as controller:
            second = controller.run(second_trigger)
        file = recording.file(second_trigger, description="The second episode at a shared instant.")
        assert second.proposal is not None
        first_orders = store.list_orders(episode_id=first.episode_id)
        assert [o.outcome_at is not None and o.outcome_at > file.as_of for o in first_orders] == [
            True
        ]
        # The first episode wrote its ledger entries, one of them for the market it bought.
        assert any(e.episode_id == first.episode_id and e.traded for e in store.ledger())

        folder = save_example(
            tmp_path / "trace",
            file,
            tape.cassette(second.episode_id),
            second,
            stored_card(world, second),
        )
        return file, replay_check(folder), [o.order_id for o in first_orders]
    finally:
        store.close()
        data.close()


def test_the_second_trace_at_a_shared_instant_holds_nothing_after_its_as_of_time_and_replays(
    data_config: Config, tmp_path: Path
) -> None:
    file, verdict, first_orders = _second_trace(data_config, tmp_path)

    # The starting state is as of the second episode's as-of time: the earlier unfilled
    # orders are there, the first episode's order, its fill and its equity point are not.
    assert {o.order_id for o in file.orders} == {f"earlier-{i}" for i in range(9)}
    assert not set(first_orders) & {o.order_id for o in file.orders}
    assert all((o.outcome_at or o.placed_at) <= file.as_of for o in file.orders)
    fills = [*file.fills, *(f for o in file.orders for f in o.fills)]
    assert all(f.filled_at <= file.as_of for f in fills)
    assert all(s.settled_at <= file.as_of for s in file.settlements)
    assert all(p.at <= file.as_of for p in file.equity)
    # Nor does the ledger tell of that order: no entry says an order filled unless the
    # file holds that order with its fills, and no entry is resolved after the as-of time.
    filled = {(o.episode_id, o.market_ticker) for o in file.orders if o.fills}
    assert all(not e.traded or (e.episode_id, e.market_ticker) in filled for e in file.ledger)
    assert all(e.resolved_at is not None and e.resolved_at <= file.as_of for e in file.ledger)

    assert verdict == "replays and matches"


def test_a_critic_rejection_from_an_earlier_episode_is_in_the_trace_and_it_replays(
    data_config: Config, tmp_path: Path
) -> None:
    claim, reason = "claim X from an earlier episode", "unsupported"
    file, verdict, _ = _second_trace(data_config, tmp_path, rejection=(claim, reason))

    # The forecaster of the second episode was shown the earlier rejection, so the replay
    # must be shown it too. The first episode made none: the scripted critic rejects nothing.
    assert file.rejections == [RecordedRejection(event_ticker=FED, claim=claim, reason=reason)]
    assert verdict == "replays and matches"
