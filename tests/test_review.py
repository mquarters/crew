"""Review runs on every open pull request, not only the crew's.

The property that makes this work is two identities: the crew can approve a
human's change, and a human can approve the crew's. Neither can approve its own.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crew_org.crews.review_crew import Finding, ReviewVerdict
from crew_org.events import EventSink
from crew_org.flows import review as review_flow
from crew_org.flows.review import REVIEW_MARKER, already_reviewed, render_review, review_open_pulls

BOT = "mqucifer-crew[bot]"
HUMAN = "mquarters"


def finding(file: str = "src/a.py") -> Finding:
    return Finding(file=file, concern="unused import", action="delete the import on line 3")


class FakeIssues:
    def __init__(self, pulls, reviews=None):
        self.owner = "mqucifer"
        self._pulls = pulls
        self._reviews = reviews or {}
        self.submitted: list[tuple[int, str, str]] = []

    def open_pulls(self, repo):
        return self._pulls

    def pull_diff(self, repo, number):
        return "diff --git a/x b/x\n+x\n"

    def pull_reviews(self, repo, number):
        return self._reviews.get(number, [])

    def create_review(self, repo, number, *, event, body):
        self.submitted.append((number, event, body))
        return {}


def pull(number=14, author=HUMAN, draft=False, title="Update README.md"):
    return {"number": number, "user": {"login": author}, "draft": draft, "title": title}


def run(issues, verdict, monkeypatch):
    monkeypatch.setattr(review_flow, "review_diff", lambda *a, **k: verdict)
    return review_open_pulls(issues, EventSink(None), repo="sprint-metrics", bot_login=BOT)


APPROVAL = ReviewVerdict(summary="Sound.", approve=True)
REJECTION = ReviewVerdict(summary="Not yet.", approve=False, findings=[finding()])


# --- the verdict schema --------------------------------------------------


def test_rejecting_without_findings_is_not_a_review():
    with pytest.raises(ValidationError, match="not a review"):
        ReviewVerdict(summary="looks wrong", approve=False, findings=[])


def test_a_finding_must_say_what_to_do():
    with pytest.raises(ValidationError, match="cannot act on"):
        Finding(file="a.py", concern="bad", action="fix")


def test_approval_needs_no_findings():
    assert ReviewVerdict(summary="Sound.", approve=True).event == "APPROVE"


# --- reviewing a human's change ------------------------------------------


def test_a_human_pull_request_is_approved_by_the_crew(monkeypatch):
    """Two identities is what makes this possible at all."""
    issues = FakeIssues([pull()])
    result = run(issues, APPROVAL, monkeypatch)
    assert issues.submitted[0][1] == "APPROVE"
    assert result.reviewed[0].approved


def test_changes_are_requested_with_the_findings_attached(monkeypatch):
    issues = FakeIssues([pull()])
    run(issues, REJECTION, monkeypatch)
    number, event, body = issues.submitted[0]
    assert event == "REQUEST_CHANGES"
    assert "delete the import" in body


# --- the crew cannot approve itself --------------------------------------


def test_the_crew_comments_rather_than_approving_its_own_work(monkeypatch):
    """GitHub forbids self-approval. The crew reviews as a second app so this
    only fires on a pull request the reviewing identity opened itself."""
    issues = FakeIssues([pull(author=BOT)])
    run(issues, APPROVAL, monkeypatch)
    assert issues.submitted[0][1] == "COMMENT"


def test_a_downgraded_verdict_is_not_reported_as_approved(monkeypatch):
    """A run printed "approved" over a review GitHub had recorded as COMMENTED,
    and the merge then waited on an approval nobody knew was missing."""
    issues = FakeIssues([pull(author=BOT)])
    result = run(issues, APPROVAL, monkeypatch)
    outcome = result.reviewed[0]
    assert outcome.event == "COMMENT"
    assert outcome.approved is False


def test_another_identity_s_pull_request_is_actually_approved(monkeypatch):
    """The whole point of the second app: the delivery bot's work gets a real
    APPROVED, so merge_approved has something to act on."""
    issues = FakeIssues([pull(author="mqucifer-crew-delivery[bot]")])
    result = run(issues, APPROVAL, monkeypatch)
    assert issues.submitted[0][1] == "APPROVE"
    assert result.reviewed[0].approved is True


def test_its_own_work_still_gets_the_findings(monkeypatch):
    issues = FakeIssues([pull(author=BOT)])
    run(issues, REJECTION, monkeypatch)
    assert "delete the import" in issues.submitted[0][2]


# --- a diff too large to read ---------------------------------------------


def test_a_diff_beyond_one_pass_is_not_approved():
    """A 30,000-character head slice meant the Reviewer approved files it had
    never seen, and its approval merges. Past the ceiling the honest verdict is
    the one a person gives: too large to review, split it."""
    from crew_org.crews.review_crew import MAX_DIFF_CHARS, review_diff

    verdict = review_diff("Enormous", "+x\n" * MAX_DIFF_CHARS)

    assert verdict.approve is False
    assert verdict.event == "REQUEST_CHANGES"
    assert verdict.findings, "a rejection has to say why"
    assert "too large" in verdict.findings[0].concern.lower()


# --- idempotency ---------------------------------------------------------


def test_an_already_reviewed_pull_request_is_skipped(monkeypatch):
    reviews = {14: [{"user": {"login": BOT}, "body": f"{REVIEW_MARKER}\nSound."}]}
    issues = FakeIssues([pull()], reviews)
    result = run(issues, APPROVAL, monkeypatch)
    assert issues.submitted == []
    assert result.skipped[0].skipped == "already reviewed"


def test_a_human_review_does_not_count_as_the_crews(monkeypatch):
    reviews = {14: [{"user": {"login": HUMAN}, "body": "looks fine to me"}]}
    issues = FakeIssues([pull()], reviews)
    run(issues, APPROVAL, monkeypatch)
    assert len(issues.submitted) == 1


def test_already_reviewed_needs_both_the_author_and_the_marker():
    assert not already_reviewed([{"user": {"login": BOT}, "body": "chat"}], BOT)
    assert not already_reviewed([{"user": {"login": HUMAN}, "body": REVIEW_MARKER}], BOT)
    assert already_reviewed([{"user": {"login": BOT}, "body": REVIEW_MARKER}], BOT)


def test_drafts_are_left_alone(monkeypatch):
    issues = FakeIssues([pull(draft=True)])
    result = run(issues, APPROVAL, monkeypatch)
    assert issues.submitted == []
    assert result.skipped[0].skipped == "draft"


def test_a_failed_review_does_not_stop_the_others(monkeypatch):
    issues = FakeIssues([pull(14), pull(15)])
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model unavailable")
        return APPROVAL

    monkeypatch.setattr(review_flow, "review_diff", flaky)
    result = review_open_pulls(issues, EventSink(None), repo="r", bot_login=BOT)
    assert result.failed[0][0] == 14
    assert result.reviewed[0].pr == 15


# --- what the author reads ----------------------------------------------


def test_the_review_names_the_file_and_the_action():
    body = render_review(REJECTION)
    assert "`src/a.py`" in body and "delete the import on line 3" in body


def test_an_approval_says_so_plainly():
    assert "No findings." in render_review(APPROVAL)
