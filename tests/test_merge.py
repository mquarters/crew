"""Landing approved work.

Merging only at sprint close meant every story branched from a main missing its
predecessors — six stories editing one module from six different starting
points, producing their conflicts all at once. Approval and merge timing are
therefore decoupled: the Sponsor's gate is the approval, and this lands what has
passed it before new work is claimed.
"""

from __future__ import annotations

from crew_org.events import EventKind, EventSink
from crew_org.flows.merge import merge_approved, ready_to_land
from crew_org.tools.github_project import Card

APPROVED = [{"state": "APPROVED"}]
COMMENTED = [{"state": "COMMENTED"}]


def card(number: int, status: str = "Merging", repo: str = "sprint-metrics") -> Card:
    return Card(
        item_id=f"C{number}",
        number=number,
        title=f"Show metric {number}",
        status=status,
        state="OPEN",
        work_type="Story",
        repo=repo,
    )


class FakeBoard:
    def __init__(self):
        self.moves = []
        self.owners = []

    def set_status(self, item_id, column):
        self.moves.append((item_id, column))

    def set_owner_agent(self, item_id, role):
        self.owners.append((item_id, role))


class FakeIssues:
    def __init__(self, *, reviews=APPROVED, mergeable_state="clean", pull=True):
        self.owner = "mqucifer"
        self._reviews, self._state, self._has_pull = reviews, mergeable_state, pull
        self.merged, self.labels, self.comments = [], [], []

    def pull_for_branch(self, repo, branch):
        return {"number": 100} if self._has_pull else None

    def pull(self, repo, number):
        return {"mergeable_state": self._state, "mergeable": self._state != "dirty"}

    def pull_reviews(self, repo, number):
        return self._reviews

    def merge_pull(self, repo, number, **kw):
        self.merged.append(number)
        return {}

    def add_labels(self, repo, number, labels):
        self.labels.extend((number, n) for n in labels)

    def comment(self, repo, number, body):
        self.comments.append((number, body))


def run(cards, issues, repos=None):
    board = FakeBoard()
    sink = EventSink(None)
    seen = []
    sink.subscribe(seen.append)
    result = merge_approved(
        board, issues, sink, cards=cards, default_repo="sprint-metrics", repos=repos
    )
    return result, board, seen


# --- selection -----------------------------------------------------------


def test_only_stories_that_passed_qa_are_landed():
    cards = [card(6), card(7, status="QAing"), card(8, status="Done")]
    assert [c.number for c in ready_to_land(cards)] == [6]


def test_repositories_outside_the_allow_list_are_left_alone():
    cards = [card(6), card(20, repo="crew")]
    assert [c.number for c in ready_to_land(cards, {"sprint-metrics"})] == [6]


# --- landing -------------------------------------------------------------


def test_an_approved_story_is_merged_and_done():
    issues = FakeIssues()
    result, board, _ = run([card(6)], issues)
    assert issues.merged == [100]
    assert result.merged == [(6, 100)]
    assert ("C6", "Done") in board.moves


def test_an_unapproved_story_waits():
    """The Sponsor's approval is the gate; QA passing is not enough."""
    issues = FakeIssues(reviews=COMMENTED)
    result, board, _ = run([card(6)], issues)
    assert issues.merged == []
    assert result.awaiting_approval == [(6, 100)]
    assert board.moves == []


def test_a_story_with_no_pull_request_is_reported():
    result, _, _ = run([card(6)], FakeIssues(pull=False))
    assert result.failed == [(6, "no open pull request")]


# --- conflicts -----------------------------------------------------------


def test_a_conflicted_pull_request_blocks_the_card():
    """A conflict means two changes disagree and a person has to decide. It is
    not something to retry around."""
    issues = FakeIssues(mergeable_state="dirty")
    result, board, seen = run([card(6)], issues)
    assert issues.merged == []
    assert result.conflicted == [(6, 100)]
    assert ("C6", "Blocked") in board.moves


def test_a_conflict_is_labelled_for_a_person():
    issues = FakeIssues(mergeable_state="dirty")
    run([card(6)], issues)
    assert (6, "blocked") in issues.labels
    assert (6, "needs:human") in issues.labels


def test_a_conflict_says_what_to_do_about_it():
    issues = FakeIssues(mergeable_state="dirty")
    run([card(6)], issues)
    _number, body = issues.comments[0]
    assert "merge conflict" in body.lower()
    assert "re-delivered" in body or "Resolve the conflict" in body


def test_a_conflict_is_visible_on_the_event_stream():
    issues = FakeIssues(mergeable_state="dirty")
    _, _, seen = run([card(6)], issues)
    assert any(e.kind == EventKind.CARD_BLOCKED for e in seen)


def test_a_failed_merge_does_not_mark_the_card_done():
    class Refusing(FakeIssues):
        def merge_pull(self, repo, number, **kw):
            raise RuntimeError("base branch was modified")

    result, board, _ = run([card(6)], Refusing())
    assert result.merged == []
    assert ("C6", "Done") not in board.moves
    assert "base branch was modified" in result.failed[0][1]
