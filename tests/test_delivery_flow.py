"""The delivery loop is where escalation discipline either holds or does not.

These tests exercise the loop rather than the policy in isolation: the policy
was always right in a unit test; what matters is that the loop asks it before
reaching for a stronger model.
"""

from __future__ import annotations

import pytest

from crew_org.crews.delivery_crew import FileWrite, Implementation
from crew_org.escalation import EscalationLedger, EscalationPolicy, FailureClass
from crew_org.events import EventKind, EventSink
from crew_org.flows import delivery
from crew_org.process import ProcessRules
from crew_org.tools.claude_code import EscalationResult, Outcome
from crew_org.tools.github_project import Card
from crew_org.tools.workspace import CheckResult, CommandResult

SPRINT = "S1"
IMPL = Implementation(
    summary="Adds cycle time",
    files=[
        FileWrite(path="src/m.py", content="def cycle():\n    return 1\n"),
        FileWrite(path="tests/test_m.py", content="def test_cycle():\n    assert True\n"),
    ],
)


def story(number: int = 6) -> Card:
    return Card(
        item_id=f"S{number}",
        number=number,
        title=f"Show metric {number}",
        status="Sprint Backlog",
        state="OPEN",
        work_type="Story",
        sprint=SPRINT,
        points=3,
        repo="sprint-metrics",
    )


def green() -> CheckResult:
    return CheckResult(results=[CommandResult(command="pytest", code=0, output="")])


def red(output: str = "2 failed") -> CheckResult:
    return CheckResult(results=[CommandResult(command="pytest", code=1, output=output)])


class FakeBoard:
    def __init__(self, cards):
        self._cards, self.moves = cards, []

    def cards(self):
        return self._cards

    def counts(self, cards=None):
        out = {}
        for c in cards if cards is not None else self._cards:
            if c.status and c.work_type not in {"Goal", "Epic"}:
                out[c.status] = out.get(c.status, 0) + 1
        return out

    def set_status(self, item_id, column):
        self.moves.append((item_id, column))


class FakeIssues:
    def __init__(self):
        self.owner, self.comments_, self.prs, self.labels = "o", [], [], []

    def get(self, repo, number):
        return {"body": "As a Sponsor…"}

    def comment(self, repo, number, body):
        self.comments_.append((number, body))

    def add_labels(self, repo, number, labels):
        self.labels.extend((number, name) for name in labels)

    def create_pull(self, repo, *, title, head, base, body):
        self.prs.append(head)
        return {"number": 100 + len(self.prs)}


class FakeWorkspace:
    def __init__(self, tmp):
        self.tmp, self.committed, self.pushed, self.closed = tmp, [], 0, 0

    def open(self, branch):
        path = self.tmp / branch.replace("/", "__")
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='x'\n")
        return path

    def commit(self, message):
        self.committed.append(message)
        return True

    def push(self):
        self.pushed += 1

    def close(self, path=None):
        self.closed += 1


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Everything the loop needs, with the model and the shell faked out."""
    calls = {"implement": 0, "escalate": 0, "feedback": []}

    def make(
        *, checks, implement=None, escalate_result=None, cards=None, dry_run=False, limit=None
    ):
        board = FakeBoard(cards or [story()])
        issues = FakeIssues()
        ws = FakeWorkspace(tmp_path)
        sequence = list(checks)

        def fake_implement(story_text, *, context, feedback=""):
            calls["implement"] += 1
            calls["feedback"].append(feedback)
            if implement:
                return implement(calls["implement"])
            return IMPL

        def fake_escalate(worktree, prompt, **kw):
            calls["escalate"] += 1
            return escalate_result or EscalationResult(outcome=Outcome.COMPLETED, detail="fixed")

        monkeypatch.setattr(delivery, "implement_story", fake_implement)
        monkeypatch.setattr(delivery.workspace, "apply", lambda w, f: [x.path for x in f])
        monkeypatch.setattr(delivery.workspace, "check", lambda w: sequence.pop(0))
        monkeypatch.setattr(delivery.claude_code, "escalate", fake_escalate)

        sink = EventSink(None)
        seen = []
        sink.subscribe(seen.append)
        org = {
            "board": {
                "columns": ["Sprint Backlog", "In Progress", "In Review", "Done"],
                "blocked_column": "Blocked",
                "human_gates": [],
            },
            "wip_limits": {"In Progress": 3},
            "sprint": {"blocked_aging_days": 3, "escalation_budget": 3},
            "execution": {"local_repair_attempts": 2},
            "escalation": {
                "never_escalate": ["SCHEMA", "SCOPE"],
                "may_escalate": ["VERIFY", "CAPABILITY"],
                "require_justification": ["CAPABILITY"],
            },
        }
        result = delivery.deliver(
            board,
            issues,
            sink,
            ProcessRules.from_config(org),
            EscalationPolicy.from_config(org),
            EscalationLedger(tmp_path / "ledger.jsonl"),
            ws,
            sprint=SPRINT,
            repo="sprint-metrics",
            dry_run=dry_run,
            limit=limit,
        )
        return result, board, issues, ws, calls, seen

    return make


# --- the happy path ------------------------------------------------------


def test_a_green_first_attempt_opens_a_pull_request(harness):
    result, board, issues, ws, calls, _ = harness(checks=[green()])
    assert len(result.delivered) == 1
    assert result.delivered[0].pr == 101
    assert calls["escalate"] == 0
    assert ws.pushed == 1
    assert ("S6", "In Review") in board.moves


def test_the_card_moves_through_in_progress_first(harness):
    _, board, _, _, _, _ = harness(checks=[green()])
    assert [m[1] for m in board.moves] == ["In Progress", "In Review"]


# --- escalation discipline ----------------------------------------------


def test_a_verify_failure_repairs_locally_before_escalating(harness):
    """Two local attempts are the contract, and the repair sees the failure."""
    result, _, _, _, calls, _ = harness(checks=[red("1 failed"), red("1 failed"), green()])
    assert calls["implement"] == 3
    assert calls["escalate"] == 0
    assert result.delivered[0].attempts == 2
    assert "1 failed" in calls["feedback"][1]


def test_escalation_happens_only_after_local_repair_is_exhausted(harness):
    result, _, _, _, calls, seen = harness(checks=[red(), red(), red(), green()])
    assert calls["escalate"] == 1
    assert result.delivered[0].escalated
    kinds = [e.kind for e in seen]
    assert EventKind.ESCALATED in kinds


def test_a_schema_failure_never_escalates(harness):
    """The model could not produce a valid implementation. That is a prompt
    defect — escalating would hide it."""

    def always_invalid(attempt):
        raise ValueError("no test file")

    result, _, issues, _, calls, _ = harness(checks=[], implement=always_invalid)
    assert calls["escalate"] == 0
    assert len(result.blocked) == 1
    assert "prompt" in result.blocked[0].blocked_reason.lower()


def test_an_escalation_is_recorded_in_the_ledger(tmp_path, harness):
    harness(checks=[red(), red(), red(), green()])
    entries = EscalationLedger(tmp_path / "ledger.jsonl").entries(SPRINT)
    assert len(entries) == 1
    assert entries[0].failure_class is FailureClass.VERIFY
    # The initial attempt plus two repairs — the retro reads this to judge
    # whether escalation is buying anything.
    assert entries[0].local_attempts == 3


# --- rate limits ---------------------------------------------------------


def test_a_usage_limit_parks_the_card_and_stops_the_run(harness):
    """Not a failure: the subscription said come back later."""
    limited = EscalationResult(outcome=Outcome.RATE_LIMITED, detail="usage limit reached")
    cards = [story(6), story(7)]
    result, board, issues, _, calls, _ = harness(
        checks=[red(), red(), red()], escalate_result=limited, cards=cards
    )
    assert result.rate_limited
    assert len(result.blocked) == 1
    # The second story is never started.
    assert calls["implement"] == 3
    assert ("S6", "Blocked") in board.moves


# --- failure handling ----------------------------------------------------


def test_a_story_that_cannot_be_finished_is_blocked_and_labelled(harness):
    failed = EscalationResult(outcome=Outcome.FAILED, detail="still broken")
    result, board, issues, _, _, _ = harness(
        checks=[red(), red(), red(), red()], escalate_result=failed
    )
    assert len(result.blocked) == 1
    assert (6, "blocked") in issues.labels
    assert ("S6", "Blocked") in board.moves


def test_the_wip_limit_stops_the_loop(harness):
    """Three in progress is the limit; a fourth waits."""
    busy = [
        Card(
            item_id=f"P{i}",
            number=200 + i,
            title="wip",
            status="In Progress",
            state="OPEN",
            work_type="Story",
        )
        for i in range(3)
    ]
    result, board, _, _, calls, seen = harness(checks=[green()], cards=[story(), *busy])
    assert result.delivered == []
    assert calls["implement"] == 0
    assert any("WIP limit" in e.summary for e in seen)


def test_the_worktree_is_always_closed(harness):
    """Even when delivery fails, the worktree must not be left behind."""
    failed = EscalationResult(outcome=Outcome.FAILED, detail="broken")
    _, _, _, ws, _, _ = harness(checks=[red(), red(), red(), red()], escalate_result=failed)
    assert ws.closed == 1


# --- dry run -------------------------------------------------------------


def test_a_dry_run_verifies_but_lands_nothing(harness, monkeypatch):
    """Same code path as a real run up to the point of landing, so what it shows
    is what would land."""
    monkeypatch.setattr(
        FakeWorkspace, "diff", lambda self: "--- a/src/m.py\n+++ b/src/m.py\n", raising=False
    )
    result, board, issues, ws, _, _ = harness(checks=[green()], dry_run=True)
    assert len(result.delivered) == 1
    outcome = result.delivered[0]
    assert outcome.diff and not outcome.landed
    assert ws.pushed == 0
    assert issues.prs == []
    assert ws.committed == []


def test_a_dry_run_leaves_the_board_as_it_found_it(harness, monkeypatch):
    monkeypatch.setattr(FakeWorkspace, "diff", lambda self: "diff", raising=False)
    _, board, _, _, _, _ = harness(checks=[green()], dry_run=True)
    assert [m[1] for m in board.moves] == ["In Progress", "Sprint Backlog"]


def test_a_dry_run_still_repairs_and_escalates(harness, monkeypatch):
    """Dry means 'does not land', not 'does not try'."""
    monkeypatch.setattr(FakeWorkspace, "diff", lambda self: "diff", raising=False)
    result, _, _, _, calls, _ = harness(checks=[red(), red(), red(), green()], dry_run=True)
    assert calls["escalate"] == 1
    assert result.delivered[0].escalated


def test_a_dry_run_failure_does_not_block_the_card(harness, monkeypatch):
    """Nothing was attempted for real, so nothing should be marked blocked."""
    failed = EscalationResult(outcome=Outcome.FAILED, detail="still broken")
    result, board, issues, _, _, _ = harness(
        checks=[red(), red(), red(), red()], escalate_result=failed, dry_run=True
    )
    assert len(result.blocked) == 1
    assert issues.labels == []
    assert "Blocked" not in [m[1] for m in board.moves]


def test_the_limit_caps_how_many_stories_are_attempted(harness):
    result, _, _, _, calls, _ = harness(
        checks=[green(), green()], cards=[story(6), story(7)], limit=1
    )
    assert len(result.delivered) == 1
    assert calls["implement"] == 1
