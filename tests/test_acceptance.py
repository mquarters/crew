"""QA judges behaviour criterion by criterion, and parents close themselves.

The rule that matters: a story cannot be accepted while any criterion is
unproven. "The suite passes" is a different claim from "every criterion is
proven", and conflating them is how Definition of Done quietly erodes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crew_org.crews.qa_crew import CriterionVerdict, QAVerdict
from crew_org.crews.retro_crew import ProcessDefect
from crew_org.events import EventSink
from crew_org.flows.acceptance import DONE, close_finished_parents, in_review, render_qa
from crew_org.tools.github_project import Card


def criterion(
    proven: bool = True, name: str = "empty sprint shows unavailable"
) -> CriterionVerdict:
    return CriterionVerdict(
        criterion=name,
        proven=proven,
        evidence="test_empty_sprint_reports_unavailable exercises it directly",
    )


# --- the acceptance rule -------------------------------------------------


def test_a_story_cannot_be_accepted_with_an_unproven_criterion():
    with pytest.raises(ValidationError, match="unproven criteria"):
        QAVerdict(summary="s", accepted=True, criteria=[criterion(True), criterion(False)])


def test_rejection_with_unproven_criteria_is_fine():
    verdict = QAVerdict(summary="s", accepted=False, criteria=[criterion(False)])
    assert len(verdict.unproven) == 1


def test_acceptance_with_every_criterion_proven_is_fine():
    assert QAVerdict(summary="s", accepted=True, criteria=[criterion(), criterion()]).accepted


def test_evidence_must_name_the_test():
    with pytest.raises(ValidationError, match="not evidence"):
        CriterionVerdict(criterion="x", proven=True, evidence="tested")


def test_a_verdict_needs_criteria_to_judge():
    with pytest.raises(ValidationError, match="list them"):
        QAVerdict(summary="s", accepted=False, criteria=[])


def test_the_qa_comment_shows_what_was_not_proven():
    body = render_qa(QAVerdict(summary="s", accepted=False, criteria=[criterion(False)]))
    assert "not proven" in body
    assert "test_empty_sprint_reports_unavailable" in body


# --- selection -----------------------------------------------------------


def story(number: int, status: str, work_type: str = "Story") -> Card:
    return Card(
        item_id=f"C{number}",
        number=number,
        title=f"Card {number}",
        status=status,
        state="OPEN",
        work_type=work_type,
        repo="sprint-metrics",
    )


def test_only_stories_in_review_are_verified():
    cards = [story(6, "In Review"), story(7, "Sprint Backlog"), story(3, "In Review", "Epic")]
    assert [c.number for c in in_review(cards)] == [6]


# --- parents close themselves -------------------------------------------


class FakeBoard:
    def __init__(self):
        self.moves = []

    def set_status(self, item_id, column):
        self.moves.append((item_id, column))


class FakeIssues:
    def __init__(self, children):
        self._children = children

    def sub_issues(self, repo, number):
        return self._children.get(number, [])


def test_an_epic_closes_when_all_its_stories_are_done():
    cards = [story(3, "Needs Refinement", "Epic"), story(6, DONE), story(7, DONE)]
    issues = FakeIssues({3: [{"number": 6}, {"number": 7}]})
    board = FakeBoard()
    closed = close_finished_parents(board, issues, EventSink(None), cards, repo="r")
    assert closed == [3]
    assert ("C3", DONE) in board.moves


def test_one_open_story_keeps_the_epic_open():
    """Close enough is not done."""
    cards = [story(3, "Needs Refinement", "Epic"), story(6, DONE), story(7, "QA")]
    issues = FakeIssues({3: [{"number": 6}, {"number": 7}]})
    board = FakeBoard()
    assert close_finished_parents(board, issues, EventSink(None), cards, repo="r") == []
    assert board.moves == []


def test_a_parent_with_no_children_is_left_alone():
    cards = [story(3, "Needs Refinement", "Epic")]
    board = FakeBoard()
    assert close_finished_parents(board, FakeIssues({}), EventSink(None), cards, repo="r") == []


def test_a_goal_closes_when_its_epics_are_done():
    cards = [story(1, "Inbox (Goals)", "Goal"), story(3, DONE, "Epic")]
    issues = FakeIssues({1: [{"number": 3}]})
    board = FakeBoard()
    assert close_finished_parents(board, issues, EventSink(None), cards, repo="r") == [1]


# --- the retro will not ask for a bigger budget -------------------------


@pytest.mark.parametrize(
    "change",
    [
        "increase the escalation budget to 6",
        "raise the budget for escalations",
        "allow more escalation budget next sprint",
    ],
)
def test_the_retro_cannot_propose_a_larger_escalation_budget(change):
    """Escalation rate is a symptom of task design. Treating it as a budget
    problem is how the discipline erodes."""
    with pytest.raises(ValidationError, match="not a process improvement"):
        ProcessDefect(subject="S1", problem="three escalations", change=change)


def test_a_real_design_change_is_accepted():
    defect = ProcessDefect(
        subject="#6",
        problem="acceptance criteria were ambiguous about empty sprints",
        change="require a worked example with concrete numbers in every criterion",
    )
    assert "worked example" in defect.change
