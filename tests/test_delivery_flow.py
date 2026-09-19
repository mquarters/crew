"""The delivery loop is where escalation discipline either holds or does not.

These tests exercise the loop rather than the policy in isolation: the policy
was always right in a unit test; what matters is that the loop asks it before
reaching for a stronger model.
"""

from __future__ import annotations

import pytest

from crew_org.crews.delivery_crew import FileEdit, FileWrite, Implementation
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
    new_files=[
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
        self.owners = []

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

    def set_owner_agent(self, item_id, role):
        self.owners.append((item_id, role))


class FakeIssues:
    def __init__(self):
        self.owner, self.comments_, self.prs, self.labels = "o", [], [], []

    def get(self, repo, number):
        return {"body": "As a Sponsor…"}

    def open_pulls(self, repo):
        return []

    def comment(self, repo, number, body):
        self.comments_.append((number, body))

    def add_labels(self, repo, number, labels):
        self.labels.extend((number, name) for name in labels)

    def create_pull(self, repo, *, title, head, base, body):
        self.prs.append(head)
        return {"number": 100 + len(self.prs)}

    def pull_for_branch(self, repo, branch):
        return None


class FakeWorkspace:
    def for_repo(self, repo):
        self.repos_asked.append(repo)
        return self

    def __init__(self, tmp):
        self.tmp, self.committed, self.pushed, self.closed = tmp, [], 0, 0
        self.repos_asked: list[str] = []
        # Recorded in order so a test can prove the evidence was read BEFORE
        # the worktree was removed, which is the whole point of keeping it.
        self.events: list[str] = []

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

    def diff(self):
        return "diff --git a/mod.py b/mod.py\n+what the model wrote\n"

    def diff_if_open(self):
        self.events.append("diff")
        return self.diff()

    def close(self, path=None):
        self.events.append("close")
        self.closed += 1


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Everything the loop needs, with the model and the shell faked out."""
    calls = {"implement": 0, "escalate": 0, "feedback": [], "context": []}

    def make(
        *,
        checks,
        implement=None,
        escalate_result=None,
        cards=None,
        apply=None,
        dry_run=False,
        limit=None,
        repos=None,
    ):
        board = FakeBoard(cards or [story()])
        issues = FakeIssues()
        ws = FakeWorkspace(tmp_path)
        sequence = list(checks)

        def fake_implement(story_text, *, context, feedback=""):
            calls["implement"] += 1
            calls["feedback"].append(feedback)
            calls["context"].append(context)
            if implement:
                return implement(calls["implement"])
            return IMPL

        def fake_escalate(worktree, prompt, **kw):
            calls["escalate"] += 1
            return escalate_result or EscalationResult(outcome=Outcome.COMPLETED, detail="fixed")

        monkeypatch.setattr(delivery, "implement_story", fake_implement)
        monkeypatch.setattr(
            delivery.workspace, "apply_implementation", apply or (lambda w, impl: [])
        )
        monkeypatch.setattr(delivery.workspace, "check", lambda w: sequence.pop(0))
        monkeypatch.setattr(delivery.claude_code, "escalate", fake_escalate)

        sink = EventSink(None)
        seen = []
        sink.subscribe(seen.append)
        org = {
            "board": {
                "columns": ["Sprint Backlog", "In Progress", "Reviewing", "QAing", "Done"],
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
            repos=repos,
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
    assert ("S6", "Reviewing") in board.moves


def test_the_card_moves_through_in_progress_first(harness):
    _, board, _, _, _, _ = harness(checks=[green()])
    assert [m[1] for m in board.moves] == ["In Progress", "Reviewing"]


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
    ledger = EscalationLedger(tmp_path / "ledger.jsonl")
    entries = ledger.entries(SPRINT)
    # The escalation, then its outcome — append-only, so both survive.
    assert len(entries) == 2
    assert ledger.outcomes(SPRINT) == {6: "resolved — lint and tests pass"}
    assert ledger.spent(SPRINT) == 1
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


# --- keeping the evidence of a failure -----------------------------------


def test_a_blocked_card_keeps_the_diff_that_failed(harness):
    """Nothing is committed or pushed until verification passes, so the worktree
    is the only copy of what the Developer wrote. Losing it means the card
    blocks with no record of why."""
    failed = EscalationResult(outcome=Outcome.FAILED, detail="broken")
    result, _, _, _, _, _ = harness(checks=[red(), red(), red(), red()], escalate_result=failed)
    assert result.blocked[0].rejected_diff is not None
    assert "what the model wrote" in result.blocked[0].rejected_diff


def test_the_evidence_is_read_before_the_worktree_is_removed(harness):
    """Ordering is the whole fix: a diff taken after close reads nothing."""
    failed = EscalationResult(outcome=Outcome.FAILED, detail="broken")
    _, _, _, ws, _, _ = harness(checks=[red(), red(), red(), red()], escalate_result=failed)
    assert ws.events == ["diff", "close"]


def test_a_blocked_card_keeps_the_whole_failure_report_not_an_extract(harness):
    """A pytest run with thirteen failures does not fit in the 400 characters
    the event log carries, and the part naming the defect is rarely the front."""
    long_output = "\n".join(f"FAILED tests/test_m.py::test_{i}" for i in range(80))
    failed = EscalationResult(outcome=Outcome.FAILED, detail="broken")
    result, _, _, _, _, _ = harness(checks=[red(long_output)] * 4, escalate_result=failed)
    detail = result.blocked[0].failure_detail
    assert detail is not None
    assert len(detail) > 400
    assert "test_79" in detail


def test_a_card_that_succeeds_keeps_no_rejected_diff(harness):
    """`rejected_diff` means a failure. Setting it on success would make `ok`
    and the evidence disagree about what happened."""
    result, _, _, _, _, _ = harness(checks=[green()])
    assert result.delivered[0].rejected_diff is None


def test_the_worktree_closed_is_the_one_that_was_opened(harness):
    """`for_repo` returns a new workspace when the repository differs. Closing
    the parent would leak the worktree and close one that never existed."""
    card = story()
    card.repo = "sprint-metrics"
    _, _, _, ws, _, _ = harness(checks=[green()], cards=[card])
    assert ws.repos_asked == ["sprint-metrics"]
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


# --- healing an interrupted run -----------------------------------------


def in_progress(number: int, title: str = "Show metric") -> Card:
    return Card(
        item_id=f"S{number}",
        number=number,
        title=f"{title} {number}",
        status="In Progress",
        state="OPEN",
        work_type="Story",
        sprint=SPRINT,
        points=3,
        repo="sprint-metrics",
    )


def test_a_card_stranded_in_progress_returns_to_the_backlog(harness, monkeypatch):
    """A tick can be killed mid-story. The board is the state, so the next pass
    heals it rather than assuming it was left tidy."""
    result, board, _, _, calls, _ = harness(checks=[green()], cards=[in_progress(6)])
    assert result.recovered == [6]
    assert ("S6", "Sprint Backlog") in board.moves


def test_a_stranded_card_with_an_open_pull_request_goes_to_review(harness, monkeypatch):
    """It is not stranded — the work landed, only the bookkeeping did not."""
    monkeypatch.setattr(
        FakeIssues,
        "open_pulls",
        lambda self, repo: [{"number": 42, "head": {"ref": "feat/6-show-metric-6"}}],
        raising=False,
    )
    result, board, _, _, _, _ = harness(checks=[green()], cards=[in_progress(6)])
    assert result.recovered == []
    assert ("S6", "Reviewing") in board.moves


def test_healing_does_not_touch_cards_that_are_where_they_belong(harness):
    result, board, _, _, _, _ = harness(checks=[green()], cards=[story(6)])
    assert result.recovered == []


def test_a_failure_event_records_why_not_only_what(harness):
    """Recording the disposition without the reason says what happened and not
    why — which is the half a retro actually needs."""
    _, _, _, _, _, seen = harness(checks=[red("2 failed in test_metrics.py"), green()])
    decided = next(e for e in seen if e.kind == EventKind.ESCALATION_DECIDED)
    assert decided.detail["failure_class"] == "VERIFY"
    assert "2 failed in test_metrics.py" in decided.detail["output"]
    assert decided.detail["reason"]
    assert decided.detail["failing_commands"] == ["pytest"]


# --- the repair must see its own work -----------------------------------


def test_the_first_attempt_is_shown_the_bodies_too(tmp_path):
    """Story #11 rewrote a function it had only ever seen the signature of, and
    was refused for breaking behaviour that lives in the body. Names say what to
    target; only the body says what the code currently promises."""
    from crew_org.flows.delivery import repository_context

    (tmp_path / "src").mkdir()
    (tmp_path / "src/mod.py").write_text(
        "def calculate_throughput(c):\n    SENTINEL = 1\n    return SENTINEL\n"
    )
    context = repository_context(tmp_path)
    assert "calculate_throughput" in context
    assert "SENTINEL" in context


def test_methods_are_shown_qualified(tmp_path):
    """The model has to know that a method is addressed as Class.method."""
    from crew_org.flows.delivery import repository_context

    (tmp_path / "src").mkdir()
    (tmp_path / "src/mod.py").write_text("class Card:\n    def age(self):\n        return 0\n")
    assert "Card.age" in repository_context(tmp_path)


def test_the_context_warns_against_targeting_a_file(tmp_path):
    """Story #8 tried to edit a definition called __init__ in __init__.py."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/__init__.py").write_text('__all__ = ["f"]\n')
    from crew_org.flows.delivery import repository_context

    assert "__all__" in repository_context(tmp_path)
    assert "not `__init__`" in repository_context(tmp_path)


def test_a_repair_is_shown_the_current_file_contents(tmp_path):
    """Regression: repairs were given the pre-implementation listing, so they
    were fixing code they could not read — two attempts failed identically
    before escalation was reached."""
    from crew_org.flows.delivery import repository_context

    (tmp_path / "src").mkdir()
    (tmp_path / "src/mod.py").write_text("SENTINEL = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_mod.py").write_text("ASSERTION = 2\n")
    context = repository_context(tmp_path)
    assert "SENTINEL" in context
    assert "ASSERTION" in context


def test_a_tree_that_does_not_fit_drops_whole_files_and_names_them(tmp_path):
    """The ceiling is a guard, not a budget. A file cut mid-function is worse
    than a file left out, because nothing in it marks where it stopped — so an
    omitted file is omitted entirely, and the Developer is told it exists."""
    from crew_org.tools import repo_context

    ceiling = repo_context.CONTEXT_CHAR_CEILING
    (tmp_path / "src").mkdir()
    # Sized off the ceiling rather than a fixed number, so raising the guard
    # does not quietly stop this from testing the guard.
    filler = "x = 1\n" * (ceiling // 60)
    for i in range(40):
        (tmp_path / f"src/mod{i}.py").write_text(f"MARKER_{i} = 1\n" + filler)
    context = repo_context.repository_context(tmp_path)

    assert len(context) < ceiling * 2
    shown = [i for i in range(40) if f"MARKER_{i} = 1" in context]
    assert shown, "nothing was shown at all"
    for i in range(40):
        # Every file is either shown whole or named as missing. Never half.
        whole = f"`src/mod{i}.py`\n\n```python\nMARKER_{i} = 1" in context
        named_missing = "not shown, because the tree did not fit" in context.lower() and (
            i not in shown
        )
        assert whole or named_missing, f"src/mod{i}.py was neither shown whole nor named"


def test_context_is_recomputed_on_every_attempt(harness):
    """Stale context is what made repairs non-convergent: a repair given the
    pre-implementation listing is fixing code it cannot read."""

    def apply(worktree, implementation):
        # Stand in for the first attempt landing in the worktree. Both passes
        # are shown the source now, so recomputation has to be proved by what
        # the source says rather than by whether it is there at all.
        (worktree / "src").mkdir(parents=True, exist_ok=True)
        (worktree / "src/written.py").write_text("FIRST_ATTEMPT = 1\n")
        return []

    _, _, _, _, calls, _ = harness(checks=[red(), green()], apply=apply)
    assert len(calls["context"]) == 2
    # The first pass has nothing written yet; the repair is shown what exists.
    assert "FIRST_ATTEMPT" not in calls["context"][0]
    assert "FIRST_ATTEMPT" in calls["context"][1]


def test_a_story_that_could_not_be_landed_says_why(harness):
    """merge_approved has always worked out why a ready story did not land, and
    the result dropped it on the floor: a run that silently skipped every merge
    looked exactly like a run with nothing to merge."""
    approved = story(6).model_copy(update={"status": "Merging"})
    result, *_ = harness(checks=[green()], cards=[approved, story(7)])

    assert result.landed == []
    assert result.unmergeable == [(6, "no open pull request")]


def test_a_dry_run_does_not_merge(harness):
    """`deliver` merged to the default branch and closed cards whatever dry_run
    said, under a command whose help reads "dry by default". A flag meaning
    "change nothing" has to mean it where the change is a merge to main."""
    approved = story(6).model_copy(update={"status": "Merging"})
    result, board, issues, _, _, _ = harness(
        checks=[green()], cards=[approved, story(7)], dry_run=True
    )

    assert result.landed == [], "nothing merged"
    assert result.would_land == [6], "and it said what it declined to merge"
    assert ("S6", "Done") not in board.moves


# --- attribution ---------------------------------------------------------


def test_the_developer_owns_the_card_it_claimed(harness):
    """The board has always had an Owner Agent field and nothing wrote it, so
    every card said a machine had acted and not which role."""
    _, board, _, _, _, _ = harness(checks=[green()])

    assert ("S6", "Developer") in board.owners


def test_healing_an_interrupted_run_claims_nothing(harness, monkeypatch):
    """Orphan reconciliation moves a card nobody decided to move. Attributing
    that to a role is a guess, and a wrong owner is worse than none."""
    stranded = story(6).model_copy(update={"status": "In Progress"})
    _, board, _, _, _, seen = harness(checks=[green()], cards=[stranded])

    assert ("S6", "Sprint Backlog") in board.moves
    assert board.owners == [], "no role stranded it, so no role claims it"
    moved = [e for e in seen if e.kind is EventKind.CARD_MOVED and e.role is None]
    assert moved, "still reported, just not attributed"
    assert moved[0].detail["from"] == "In Progress"


# --- the regression guard in the loop -----------------------------------


OVERWRITING = Implementation(
    summary="Rewrites the module",
    new_files=[
        FileWrite(path="src/m.py", content="def only_the_new_thing():\n    return 1\n"),
        FileWrite(path="tests/test_m.py", content="def test_x():\n    assert True\n"),
    ],
)


def test_writing_over_an_existing_file_is_refused(harness, monkeypatch, tmp_path):
    """A 'new file' that already exists is a whole-file rewrite by another name."""

    def seeded_open(self, branch):
        path = tmp_path / branch.replace("/", "__")
        (path / "src").mkdir(parents=True, exist_ok=True)
        (path / "src/m.py").write_text("def already_merged():\n    return 0\n")
        (path / "tests").mkdir(parents=True, exist_ok=True)
        (path / "tests/test_m.py").write_text("def test_old():\n    assert True\n")
        return path

    monkeypatch.setattr(FakeWorkspace, "open", seeded_open, raising=False)

    editing = Implementation(
        summary="Adds throughput alongside what is there",
        edits=[
            FileEdit(
                path="src/m.py",
                operation="add",
                target="throughput",
                source="def throughput():\n    return 1",
            ),
            FileEdit(
                path="tests/test_m.py",
                operation="add",
                target="test_throughput",
                source="def test_throughput():\n    assert True",
            ),
        ],
    )
    result, _, _, _, calls, seen = harness(
        checks=[green()], implement=lambda n: OVERWRITING if n == 1 else editing
    )
    assert calls["implement"] == 2
    assert result.delivered
    decided = [e for e in seen if e.kind == EventKind.ESCALATION_DECIDED]
    assert decided[0].detail["failure_class"] == "OVERWRITE"
    assert "src/m.py" in decided[0].detail["paths"]


# --- which repositories the crew may work in ----------------------------


def other_repo_story(number: int = 20, repo: str = "crew") -> Card:
    return Card(
        item_id=f"S{number}",
        number=number,
        title=f"Orchestrator work {number}",
        status="Sprint Backlog",
        state="OPEN",
        work_type="Story",
        sprint=SPRINT,
        points=3,
        repo=repo,
    )


def test_a_card_outside_the_allow_list_is_never_claimed(harness):
    """It belongs on the board — refined, prioritised, visible — but it is not
    the crew's to implement."""
    result, board, _, _, calls, _ = harness(
        checks=[green()], cards=[other_repo_story()], repos={"sprint-metrics"}
    )
    assert calls["implement"] == 0
    assert board.moves == []
    assert result.not_ours == [20]


def test_cards_inside_the_allow_list_are_worked(harness):
    result, _, _, _, calls, _ = harness(
        checks=[green()], cards=[story(6)], repos={"sprint-metrics"}
    )
    assert calls["implement"] == 1
    assert result.not_ours == []


def test_a_mixed_sprint_works_only_what_it_may(harness):
    result, _, _, _, calls, _ = harness(
        checks=[green()], cards=[other_repo_story(20), story(6)], repos={"sprint-metrics"}
    )
    assert calls["implement"] == 1
    assert result.delivered[0].card == 6
    assert result.not_ours == [20]


def test_work_happens_in_the_cards_own_repository(harness):
    """A global default would implement a card belonging to one repo inside
    another, silently."""
    _, _, _, ws, _, _ = harness(checks=[green()], cards=[story(6)])
    assert ws.repos_asked == ["sprint-metrics"]


def test_without_an_allow_list_nothing_is_excluded(harness):
    """Absent configuration should not silently stop the crew working."""
    result, _, _, _, calls, _ = harness(checks=[green()], cards=[story(6)])
    assert calls["implement"] == 1
    assert result.not_ours == []


def test_a_fumbled_name_does_not_spend_the_budget_for_real_failures(harness):
    """Story #8: two invented definition names left the first genuine test
    failure with nothing in hand, so it escalated immediately."""
    from crew_org.tools.ast_edit import EditError

    calls = {"n": 0}

    def sometimes_unapplicable(worktree, implementation):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise EditError("no definition named 'invented'")
        return []

    # Two edit failures, then one test failure, then green.
    result, _, _, _, _, seen = harness(checks=[red(), green()], apply=sometimes_unapplicable)

    edits = [e for e in seen if e.detail.get("failure_class") == "EDIT"]
    verifies = [e for e in seen if e.detail.get("failure_class") == "VERIFY"]
    assert len(edits) == 2
    # The VERIFY failure still gets a repair rather than escalating on arrival.
    assert verifies and "retry_local" in verifies[0].summary
    assert result.delivered
