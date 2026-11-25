"""The beam's ordering rule, evidence support, and the pruning audit."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pmfa.forecast import build_outcome_space, pruning_audit
from pmfa.forecast.support import beam_order, evidence_support
from pmfa.records.forecast import NodeStatus, TreeNode

from .scripted import AS_OF, ORIGINS, PASSAGES, load_event, origin, series


def _node(nid: str, mass: float, *, origins: int = 1, days_old: float = 1.0, support: float = 1.0):
    return TreeNode(
        node_id=nid,
        episode_id="ep",
        parent_id=None,
        depth=1,
        scheduled_event_id="lv",
        claim=nid,
        status=NodeStatus.SCORED,
        path_mass=mass,
        cond_probability=mass,
        evidence_support=support,
        n_origins=origins,
        freshest_evidence_at=AS_OF - timedelta(days=days_old),
    )


def test_beam_takes_the_highest_mass_times_support():
    nodes = [_node("a", 0.2), _node("b", 0.9, support=0.5), _node("c", 0.3)]
    assert [n.node_id for n in beam_order(nodes, 0.05)] == ["b", "c", "a"]  # 0.45, 0.3, 0.2


def test_ties_within_the_band_go_to_more_origins_then_fresher_evidence_then_score_then_id():
    nodes = [
        _node("a", 0.50, origins=1),
        _node("b", 0.48, origins=2, days_old=5),
        _node("c", 0.47, origins=2, days_old=1),
        _node("d", 0.30, origins=3),
        _node("f", 0.10),
        _node("e", 0.10),
    ]
    order = [n.node_id for n in beam_order(nodes, 0.05)]
    # c and b have more origins than a and are within 0.05 of it; c is fresher.
    # d has the most origins but is outside the band until a, b and c are taken.
    assert order == ["c", "b", "a", "d", "e", "f"]
    assert [n.node_id for n in beam_order(nodes, 0.0)] == ["a", "b", "c", "d", "e", "f"]


def test_evidence_support_weights_primary_origins_and_decays_with_age():
    passages = {p.passage_id: p for p in PASSAGES}
    origins = {o.origin_id: o for o in ORIGINS}
    primary = evidence_support(["p1"], passages, origins, AS_OF, 30.0)
    assert primary.value == pytest.approx(0.5 ** (2 / 30))  # weight 2, two days old, / 2
    secondary = evidence_support(["p2"], passages, origins, AS_OF, 30.0)
    assert secondary.value == pytest.approx(0.5 * 0.5 ** (2 / 30))
    both = evidence_support(["p1", "p2", "p3"], passages, origins, AS_OF, 30.0)
    assert both.value == 1.0 and both.n_origins == 3
    assert both.freshest == origins["o1"].published_at
    same_origin = evidence_support(["p1", "p_late"], passages, origins, AS_OF, 30.0)
    assert same_origin.n_origins == 1  # passages from one origin are not independent
    assert evidence_support(["p_orphan"], passages, origins, AS_OF, 30.0).value == 0.0
    old = {"o3": origin("o3", primary=False, days_old=30)}
    assert evidence_support(["p3"], passages, old, AS_OF, 30.0).value == pytest.approx(0.25)


def _scored(nid: str, mass: float, dist: dict[str, float], status: NodeStatus, parent=None):
    return TreeNode(
        node_id=nid,
        episode_id="ep",
        parent_id=parent,
        depth=1 if parent is None else 2,
        scheduled_event_id="lv",
        claim=nid,
        status=status,
        path_mass=mass,
        outcome_probabilities=dist,
    )


def test_pruning_audit_measures_posterior_mass_on_frozen_scenarios():
    space = build_outcome_space(load_event("KXFEDDECISION-26DEC"), series("KXFEDDECISION"))
    hold, cut = {"hold": 1.0}, {"cut_25": 0.8, "hold": 0.2}
    nodes = [
        _scored("a", 0.6, hold, NodeStatus.EXPANDED),
        _scored("a1", 0.6, hold, NodeStatus.LEAF, parent="a"),
        _scored("b", 0.3, cut, NodeStatus.FROZEN),
        _scored("c", 0.1, cut, NodeStatus.FROZEN),
        _scored("gone", 0.5, cut, NodeStatus.DELETED),
    ]
    audit = pruning_audit(nodes, space, "cut_25")
    assert audit.frozen_posterior_mass == pytest.approx(1.0)
    assert audit.most_likely_node_id == "b" and audit.most_likely_was_frozen
    assert audit.n_scenarios == 3 and audit.n_frozen == 2

    audit = pruning_audit(nodes, space, "hold")
    assert audit.frozen_posterior_mass == pytest.approx(0.08 / 0.68)
    assert audit.most_likely_node_id == "a1" and not audit.most_likely_was_frozen

    with pytest.raises(ValueError):
        pruning_audit(nodes, space, "pause")
