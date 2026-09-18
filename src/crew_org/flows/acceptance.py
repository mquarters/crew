"""Acceptance: verifying delivered work, and closing out what is finished.

QA judges behaviour against the acceptance criteria, which is a different
question from the Reviewer's. A story only leaves In Review when every
criterion is proven by a test that actually exercises it.

Parent completion is bookkeeping the crew should not make a human do: an epic
whose stories are all Done is done, and so is a goal whose epics are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from crew_org.crews.qa_crew import QAVerdict, verify_story
from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.git_ops import Workspace, branch_name
from crew_org.tools import workspace
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient
from crew_org.tools.sandbox import Sandbox

IN_PROGRESS = "In Progress"
IN_REVIEW = "In Review"
QA = "QA"
DONE = "Done"
STORY_TYPE = "Story"
EPIC_TYPE = "Epic"
GOAL_TYPE = "Goal"

QA_MARKER = "<!-- crew:qa -->"


@dataclass
class QAOutcome:
    card: int
    accepted: bool
    unproven: int = 0
    reason: str | None = None


@dataclass
class AcceptanceResult:
    verified: list[QAOutcome] = field(default_factory=list)
    returned: list[QAOutcome] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    parents_closed: list[int] = field(default_factory=list)


def in_review(cards: list[Card]) -> list[Card]:
    return [
        c
        for c in cards
        if c.status == IN_REVIEW and c.work_type == STORY_TYPE and c.state != "CLOSED"
    ]


def render_qa(verdict: QAVerdict) -> str:
    lines = [
        QA_MARKER,
        f"## QA — {'accepted' if verdict.accepted else 'not accepted'}",
        "",
        verdict.summary,
        "",
        "### Criteria",
        "",
    ]
    for item in verdict.criteria:
        mark = "proven" if item.proven else "**not proven**"
        lines += [f"- {mark} — {item.criterion}", f"  - {item.evidence}"]
    return "\n".join(lines)


def collect_tests(worktree: Path) -> str:
    """The test code, which is the evidence QA reasons about."""
    parts = []
    for path in sorted(worktree.rglob("test_*.py")):
        if ".venv" in path.parts:
            continue
        parts.append(f"# {path.relative_to(worktree)}\n{path.read_text()}")
    return "\n\n".join(parts)


def run_qa(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    ws: Workspace,
    sandbox: Sandbox,
    *,
    cards: list[Card],
    repo: str,
) -> AcceptanceResult:
    """Verify everything sitting In Review."""
    result = AcceptanceResult()

    for card in in_review(cards):
        number = card.number or 0
        if issues.has_comment_marked(repo, number, QA_MARKER):
            continue

        branch = branch_name(number, card.title)
        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_STARTED,
                role="QA Engineer",
                card=number,
                summary=f"verify {card.title[:46]}",
            )
        )
        try:
            worktree = ws.open_existing(branch)
            check = workspace.check(worktree, sandbox=sandbox)
            verdict = verify_story(
                f"{card.title}\n\n{issues.get(repo, number).get('body') or ''}",
                test_output="\n\n".join(f"$ {r.command}\n{r.output}" for r in check.results)[:4000],
                test_code=collect_tests(worktree),
            )
        except Exception as exc:  # noqa: BLE001
            result.failed.append((number, f"{type(exc).__name__}: {exc}"))
            sink.emit(
                CrewEvent(
                    kind=EventKind.AGENT_FAILED,
                    role="QA Engineer",
                    card=number,
                    summary=str(exc)[:80],
                )
            )
            continue
        finally:
            ws.close()

        issues.comment(repo, number, render_qa(verdict))

        if verdict.accepted:
            board.set_status(card.item_id, QA)
            result.verified.append(QAOutcome(card=number, accepted=True))
        else:
            board.set_status(card.item_id, IN_PROGRESS)
            outcome = QAOutcome(
                card=number,
                accepted=False,
                unproven=len(verdict.unproven),
                reason=verdict.unproven[0].evidence if verdict.unproven else None,
            )
            result.returned.append(outcome)

        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_FINISHED,
                role="QA Engineer",
                card=number,
                summary=f"{'accepted' if verdict.accepted else 'returned'} — "
                f"{len(verdict.unproven)} unproven",
                detail={
                    "accepted": verdict.accepted,
                    "unproven": [c.criterion for c in verdict.unproven],
                },
            )
        )

    return result


def close_finished_parents(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    cards: list[Card],
    *,
    repo: str,
) -> list[int]:
    """Move an epic to Done when its stories are, and a goal when its epics are.

    Bookkeeping a human should not have to do. Done means every child is Done —
    a parent with one story still open is not finished, however close it looks.
    """
    closed: list[int] = []
    by_number = {c.number: c for c in cards}

    for parent_type in (EPIC_TYPE, GOAL_TYPE):
        for card in cards:
            if card.work_type != parent_type or card.status == DONE or card.state == "CLOSED":
                continue
            try:
                children = issues.sub_issues(card.repo or repo, card.number or 0)
            except Exception:  # noqa: BLE001
                continue
            if not children:
                continue

            statuses = [
                by_number[child["number"]].status
                for child in children
                if child["number"] in by_number
            ]
            if not statuses or any(status != DONE for status in statuses):
                continue

            board.set_status(card.item_id, DONE)
            closed.append(card.number or 0)
            sink.emit(
                CrewEvent(
                    kind=EventKind.CARD_MOVED,
                    card=card.number,
                    summary=f"all {len(statuses)} children done — closing {parent_type.lower()}",
                    **{"to": DONE},
                )
            )
    return closed
