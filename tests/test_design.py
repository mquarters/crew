"""Design is rationed because the Architect is the escalation bottleneck, so the
rationing rules are tested as rules."""

from __future__ import annotations

import pytest

from crew_org.config import load_org
from crew_org.design import DesignPolicy, EpicShape

FORCE = "needs:design"
SKIP = "no:design"


@pytest.fixture
def policy() -> DesignPolicy:
    return DesignPolicy(
        min_points=13, min_stories=4, min_modules=3, force_label=FORCE, skip_label=SKIP
    )


def test_small_epic_skips_design(policy):
    d = policy.decide(EpicShape(points_total=5, story_count=2, modules_touched=1))
    assert not d.required


@pytest.mark.parametrize(
    "shape",
    [
        EpicShape(points_total=13, story_count=1),
        EpicShape(points_total=1, story_count=4),
        EpicShape(points_total=1, story_count=1, modules_touched=3),
    ],
)
def test_any_single_threshold_triggers_design(policy, shape):
    assert policy.decide(shape).required


def test_thresholds_are_inclusive_at_the_boundary(policy):
    assert policy.decide(EpicShape(points_total=13, story_count=0)).required
    assert not policy.decide(EpicShape(points_total=12, story_count=0)).required


def test_force_label_demands_design_on_a_tiny_epic(policy):
    d = policy.decide(EpicShape(points_total=1, story_count=1, labels=frozenset({FORCE})))
    assert d.required
    assert FORCE in d.reason


def test_skip_label_waives_design_on_a_large_epic(policy):
    d = policy.decide(EpicShape(points_total=99, story_count=20, labels=frozenset({SKIP})))
    assert not d.required


def test_force_beats_skip_because_demanding_design_is_the_safer_error(policy):
    d = policy.decide(EpicShape(points_total=1, story_count=1, labels=frozenset({FORCE, SKIP})))
    assert d.required


def test_reason_is_always_specific_enough_to_audit(policy):
    for shape in (
        EpicShape(points_total=2, story_count=1),
        EpicShape(points_total=21, story_count=9),
    ):
        assert any(ch.isdigit() for ch in policy.decide(shape).reason)


def test_shipped_config_builds_a_working_design_policy():
    policy = DesignPolicy.from_config(load_org())
    assert not policy.decide(EpicShape(points_total=3, story_count=1)).required
    assert policy.decide(EpicShape(points_total=34, story_count=8, modules_touched=5)).required
