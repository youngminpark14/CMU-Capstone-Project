"""An episode that ends before the forecast step never calls the Generator or the Critic.

The cheap roles (Monitor, Researcher) run on every trigger; the expensive ones
run only on what passes them. Each case below ends at matching or research and
asserts that no scenario was generated or scored.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from pmfa.records.episode import Exit
from pmfa.records.roles import RoleTask

from .support import (
    AS_OF,
    NEWS,
    Script,
    doc_trigger,
    fed_file,
    hawkish_script,
    run_episode,
    syndicated,
)


def _no_match():
    return fed_file(), Script(matches=[None])


def _no_evidence():
    return fed_file(), hawkish_script(keep=lambda payload: [])


def _insufficient_origins():
    copies = [syndicated(NEWS, 1, "wire.example"), syndicated(NEWS, 2, "paper.example")]
    file = fed_file(
        documents=[NEWS, *copies], trigger=doc_trigger(NEWS, at=AS_OF + timedelta(hours=1))
    )
    return file, hawkish_script()


@pytest.mark.parametrize(
    ("case", "exit"),
    [
        (_no_match, Exit.NO_MATCH),
        (_no_evidence, Exit.NO_EVIDENCE),
        (_insufficient_origins, Exit.INSUFFICIENT_ORIGINS),
    ],
)
def test_an_episode_that_ends_before_the_forecast_makes_no_generator_or_critic_call(case, exit):
    file, script = case()
    record, world = run_episode(file, script)
    assert record.exit is exit
    assert record.forecast is None
    assert world.models.calls_for(RoleTask.GENERATE) == []
    assert world.models.calls_for(RoleTask.SCORE) == []
    assert world.store.list_orders() == []
