"""Sprint planning is mechanical because approving an epic was the scope
decision. These tests pin the mechanics, and the reporting level: the Sponsor
sees epics, never a list of stories."""

from __future__ import annotations

from crew_org.flows.sprint import INBOX, READY, approved_epics, plan_sprint
from crew_org.tools.github_project import Card


def epic(number: int, priority: str = "P1", status: str = "Needs Refinement") -> Card:
    return Card(
        item_id=f"E{number}",
        number=number,
        title=f"Epic {number}",
        status=status,
        state="OPEN",
        work_type="Epic",
        priority=priority,
    )


def story(number: int, points: int, status: str = READY) -> Card:
    return Card(
        item_id=f"S{number}",
        number=number,
        title=f"Story {number}",
        status=status,
        state="OPEN",
        work_type="Story",
        points=points,
    )


# --- which epics count ---------------------------------------------------


def test_epics_still_at_the_gate_are_not_in_scope():
    cards = [epic(3), epic(5, status=INBOX)]
    assert [e.number for e in approved_epics(cards)] == [3]


def test_closed_epics_are_not_in_scope():
    rejected = epic(9)
    rejected.state = "CLOSED"
    assert approved_epics([epic(3), rejected]) == [approved_epics([epic(3)])[0]]


def test_epics_are_taken_in_priority_order():
    cards = [epic(3, "P2"), epic(4, "P0"), epic(5, "P1")]
    assert [e.number for e in approved_epics(cards)] == [4, 5, 3]


def test_unprioritised_epics_sort_last():
    """Unprioritised work is not urgent — it is unconsidered."""
    unset = epic(7)
    unset.priority = None
    assert [e.number for e in approved_epics([unset, epic(3, "P3")])] == [3, 7]


# --- filling the sprint --------------------------------------------------


def test_a_sprint_that_fits_takes_everything():
    cards = [epic(3), story(6, 3), story(7, 2)]
    plan = plan_sprint(cards, {6: 3, 7: 3}, sprint="S1", capacity=20)
    assert plan.admitted == [6, 7]
    assert plan.points == 5
    assert plan.slices[0].complete


def test_capacity_stops_the_fill():
    cards = [epic(3), story(6, 8), story(7, 8), story(8, 8)]
    plan = plan_sprint(cards, {6: 3, 7: 3, 8: 3}, sprint="S1", capacity=20)
    assert plan.admitted == [6, 7]
    assert plan.slices[0].deferred == [8]
    assert not plan.slices[0].complete


def test_a_story_is_never_split_to_fit():
    """A partially admitted story is not deliverable; shaving scope by halves is
    how a sprint rots."""
    cards = [epic(3), story(6, 8), story(7, 5)]
    plan = plan_sprint(cards, {6: 3, 7: 3}, sprint="S1", capacity=10)
    assert plan.admitted == [6]
    assert plan.points == 8


def test_a_later_smaller_story_still_fits_after_a_deferral():
    """Capacity is a budget, not a cursor: passing on one story does not close
    the sprint to everything after it."""
    cards = [epic(3), story(6, 8), story(7, 1)]
    plan = plan_sprint(cards, {6: 3, 7: 3}, sprint="S1", capacity=8)
    assert plan.admitted == [6]
    plan2 = plan_sprint(cards, {6: 3, 7: 3}, sprint="S1", capacity=9)
    assert plan2.admitted == [6, 7]


def test_a_higher_priority_epic_is_filled_first():
    cards = [epic(3, "P2"), epic(4, "P0"), story(6, 8), story(9, 8)]
    plan = plan_sprint(cards, {6: 3, 9: 4}, sprint="S1", capacity=8)
    assert plan.admitted == [9]


def test_only_ready_stories_are_admitted():
    cards = [epic(3), story(6, 3), story(7, 3, status="Needs Refinement")]
    plan = plan_sprint(cards, {6: 3, 7: 3}, sprint="S1", capacity=20)
    assert plan.admitted == [6]


def test_stories_without_a_parent_epic_are_reported_not_admitted():
    """An orphan story has not been through an approved epic, so nobody agreed
    to it."""
    cards = [epic(3), story(6, 3), story(99, 3)]
    plan = plan_sprint(cards, {6: 3}, sprint="S1", capacity=20)
    assert plan.admitted == [6]
    assert plan.unparented == [99]


def test_an_epic_with_no_ready_stories_is_left_out_of_the_report():
    cards = [epic(3), epic(4), story(6, 3)]
    plan = plan_sprint(cards, {6: 3}, sprint="S1", capacity=20)
    assert [p.number for p in plan.slices] == [3]


def test_the_plan_reports_per_epic_not_per_story():
    """The Sponsor's unit of attention is the epic."""
    cards = [epic(3), epic(4), story(6, 3), story(9, 2)]
    plan = plan_sprint(cards, {6: 3, 9: 4}, sprint="S1", capacity=20)
    assert len(plan.slices) == 2
    assert [(p.number, p.points) for p in plan.slices] == [(3, 3), (4, 2)]
