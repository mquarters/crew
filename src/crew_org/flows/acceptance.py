"""Acceptance: verifying delivered work, and closing out what is finished.

QA judges behaviour against the acceptance criteria, which is a different
question from the Reviewer's. A story only leaves Awaiting QA when every
criterion is proven by a test that actually exercises it.

The columns say what a card is waiting for, not what is happening to it. A
card sits in Awaiting QA until QA has finished with it — QA does not pull it
into a lane of its own — and lands in Awaiting Approval already verified, where
what it waits for is the Sponsor.

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

# QA reasons about the test code, so what it is shown decides its verdict. A
# 12,000-character slice in the prompt cut story #13's two new tests off the end
# of a 13,839-character file, and QA correctly reported that it could not find
# them. New tests are appended, so a head-slice lands on the evidence every
# time.
#
# Past this, QA refuses to judge rather than judging on part of the evidence.
# QAVerdict has no way to say "I could not see enough to tell" — `proven` is a
# bool — so incomplete evidence has to resolve to proven or unproven, and both
# are false. Asking the model in prose not to read an omission as an absence is
# worse still: it reads equally well as "assume it is covered", which turns a
# truncation into an acceptance in the gate that now merges without a person.
QA_CONTEXT_CHAR_CEILING = 200_000
# The end of a test run is where the summary and the failures are. Keeping the
# front of it is the same mistake delivery already learned not to make.
QA_OUTPUT_CHAR_CEILING = 40_000

IN_PROGRESS = "In Progress"
AWAITING_QA = "Awaiting QA"
AWAITING_APPROVAL = "Awaiting Approval"
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


class EvidenceTooLarge(RuntimeError):
    """The tests did not fit, so there is no honest verdict to give."""


@dataclass
class AcceptanceResult:
    verified: list[QAOutcome] = field(default_factory=list)
    returned: list[QAOutcome] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    parents_closed: list[int] = field(default_factory=list)


def awaiting_qa(cards: list[Card]) -> list[Card]:
    return [
        c
        for c in cards
        if c.status == AWAITING_QA and c.work_type == STORY_TYPE and c.state != "CLOSED"
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
    """The test code, which is the evidence QA reasons about.

    Every test file, whole, or EvidenceTooLarge. A cut that lands inside a test
    function shows QA half a test and no sign that there was more, and a
    verdict reached on part of the evidence is not a verdict.
    """
    parts: list[str] = []
    total = 0
    for path in sorted(worktree.rglob("test_*.py")):
        if ".venv" in path.parts:
            continue
        rel = path.relative_to(worktree)
        body = path.read_text(encoding="utf-8", errors="ignore")
        total += len(body)
        if total > QA_CONTEXT_CHAR_CEILING:
            raise EvidenceTooLarge(
                f"the tests are larger than {QA_CONTEXT_CHAR_CEILING:,} characters "
                f"(reached at {rel}), so no criterion can be judged on all of the "
                "evidence. Raise QA_CONTEXT_CHAR_CEILING or split the suite."
            )
        parts.append(f"# {rel}\n{body}")
    return "\n\n".join(parts)


def collect_output(results) -> str:
    """What running the suite produced, keeping the end rather than the front."""
    joined = "\n\n".join(f"$ {r.command}\n{r.output}" for r in results)
    if len(joined) <= QA_OUTPUT_CHAR_CEILING:
        return joined
    return "…earlier output trimmed…\n" + joined[-QA_OUTPUT_CHAR_CEILING:]


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
    """Verify everything sitting in Awaiting QA."""
    result = AcceptanceResult()

    for card in awaiting_qa(cards):
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
                test_output=collect_output(check.results),
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
            board.set_status(card.item_id, AWAITING_APPROVAL)
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
