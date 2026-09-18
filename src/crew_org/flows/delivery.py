"""Delivery: a story in the sprint becomes a pull request.

The loop is deliberately narrow. A Developer produces a typed implementation,
it is written into an isolated worktree, and the worktree is linted and tested.
If that fails, the failure is *classified* before anything else happens — which
is what keeps escalation from becoming the easy path.

Only VERIFY failures escalate, and only after local repair is exhausted. A
SCHEMA failure means the prompt or schema is wrong and gets repaired locally; a
SCOPE failure means the story was not ready and goes back to refinement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from crew_org.crews.delivery_crew import Implementation, implement_story
from crew_org.escalation import (
    Disposition,
    EscalationLedger,
    EscalationPolicy,
    EscalationRecord,
    FailureClass,
    LocalFailure,
    utcnow,
)
from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.git_ops import Workspace, branch_name
from crew_org.process import ProcessRules
from crew_org.tools import claude_code, workspace
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient

SPRINT_BACKLOG = "Sprint Backlog"
IN_PROGRESS = "In Progress"
IN_REVIEW = "In Review"
BLOCKED = "Blocked"
STORY_TYPE = "Story"

# Files worth showing the Developer so it writes code that fits in.
CONTEXT_FILES = ("pyproject.toml", "README.md")
MAX_CONTEXT_FILES = 40


@dataclass
class DeliveryOutcome:
    card: int
    branch: str | None = None
    pr: int | None = None
    blocked_reason: str | None = None
    attempts: int = 0
    escalated: bool = False

    @property
    def ok(self) -> bool:
        return self.pr is not None


@dataclass
class DeliveryResult:
    delivered: list[DeliveryOutcome] = field(default_factory=list)
    blocked: list[DeliveryOutcome] = field(default_factory=list)
    rate_limited: bool = False


def sprint_stories(cards: list[Card], sprint: str) -> list[Card]:
    """Stories in this sprint waiting to be picked up."""
    return sorted(
        (
            c
            for c in cards
            if c.status == SPRINT_BACKLOG
            and c.work_type == STORY_TYPE
            and c.state != "CLOSED"
            and (c.sprint == sprint or sprint is None)
        ),
        key=lambda c: c.number or 0,
    )


def repository_context(worktree: Path) -> str:
    """What the repository looks like, so the Developer writes code that fits."""
    paths = sorted(
        str(p.relative_to(worktree))
        for p in worktree.rglob("*")
        if p.is_file()
        and ".git" not in p.parts
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
    )
    lines = ["### Files", ""]
    lines += [f"- {p}" for p in paths[:MAX_CONTEXT_FILES]]
    if len(paths) > MAX_CONTEXT_FILES:
        lines.append(f"- … and {len(paths) - MAX_CONTEXT_FILES} more")

    for name in CONTEXT_FILES:
        target = worktree / name
        if target.exists():
            lines += ["", f"### {name}", "", "```", target.read_text()[:2500].strip(), "```"]
    return "\n".join(lines)


def escalation_prompt(story: str, failure: str) -> str:
    return (
        "You are finishing work a smaller model could not complete, in a git worktree.\n\n"
        f"## The story\n\n{story}\n\n"
        f"## What is failing\n\n```\n{failure}\n```\n\n"
        "Fix the implementation so the tests and lint pass. Write the test that "
        "expresses each acceptance criterion if it is missing. Stay inside the story's "
        "scope — anything else you notice belongs in a new issue, not this change.\n"
        "Do not commit or push; the crew handles that."
    )


def deliver_story(
    card: Card,
    *,
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    ws: Workspace,
    policy: EscalationPolicy,
    ledger: EscalationLedger,
    sprint: str,
    repo: str,
    default_branch: str,
) -> DeliveryOutcome:
    """Take one story from Sprint Backlog to a pull request."""
    number = card.number or 0
    outcome = DeliveryOutcome(card=number)
    story_text = f"{card.title}\n\n{issues.get(repo, number).get('body') or ''}"

    branch = branch_name(number, card.title)
    outcome.branch = branch
    worktree = ws.open(branch)
    sink.emit(
        CrewEvent(kind=EventKind.AGENT_STARTED, role="Developer", card=number, summary=branch)
    )

    context = repository_context(worktree)
    feedback = ""
    implementation: Implementation | None = None

    while True:
        try:
            implementation = implement_story(story_text, context=context, feedback=feedback)
        except Exception as exc:  # noqa: BLE001
            # The model could not produce a valid implementation at all.
            failure = LocalFailure(
                card=number,
                role="Developer",
                failure_class=FailureClass.SCHEMA,
                attempts=outcome.attempts,
                detail=str(exc)[:400],
            )
            decision = policy.decide(failure, spent=ledger.spent(sprint))
            outcome.attempts += 1
            sink.emit(
                CrewEvent(
                    kind=EventKind.ESCALATION_DECIDED,
                    role="Developer",
                    card=number,
                    summary=f"SCHEMA — {decision.disposition}",
                )
            )
            if decision.disposition is Disposition.RETRY_LOCAL:
                feedback = f"Your output did not validate:\n{exc}"
                continue
            outcome.blocked_reason = decision.reason
            return outcome

        workspace.apply(worktree, implementation.files)
        check = workspace.check(worktree)
        if check.ok:
            break

        failure = LocalFailure(
            card=number,
            role="Developer",
            failure_class=FailureClass.VERIFY,
            attempts=outcome.attempts,
            detail=check.failure_report[:400],
        )
        decision = policy.decide(failure, spent=ledger.spent(sprint))
        outcome.attempts += 1
        sink.emit(
            CrewEvent(
                kind=EventKind.ESCALATION_DECIDED,
                role="Developer",
                card=number,
                summary=f"VERIFY — {decision.disposition}",
            )
        )

        if decision.disposition is Disposition.RETRY_LOCAL:
            feedback = f"Lint or tests failed:\n\n{check.failure_report}"
            continue

        if decision.disposition is Disposition.ESCALATE:
            ledger.record(
                EscalationRecord(
                    at=utcnow(),
                    sprint=sprint,
                    card=number,
                    role="Developer",
                    failure_class=FailureClass.VERIFY,
                    local_attempts=outcome.attempts,
                    justification=None,
                    detail=check.failure_report[:400],
                )
            )
            outcome.escalated = True
            sink.emit(
                CrewEvent(
                    kind=EventKind.ESCALATED,
                    role="Developer",
                    card=number,
                    summary="local repair exhausted",
                    failure_class="VERIFY",
                )
            )
            result = claude_code.escalate(
                worktree, escalation_prompt(story_text, check.failure_report)
            )
            if result.should_park:
                outcome.blocked_reason = result.detail
                return outcome
            check = workspace.check(worktree)
            if check.ok:
                break
            outcome.blocked_reason = f"escalation did not resolve it: {check.failure_report[:200]}"
            return outcome

        outcome.blocked_reason = decision.reason
        return outcome

    if not ws.commit(
        f"feat({number}): {card.title}\n\n{implementation.summary}\n\nCloses #{number}"
    ):
        outcome.blocked_reason = "the implementation produced no change"
        return outcome
    ws.push()

    pr = issues.create_pull(
        repo,
        title=card.title,
        head=branch,
        base=default_branch,
        body=_pr_body(card, implementation, outcome),
    )
    outcome.pr = pr["number"]
    sink.emit(
        CrewEvent(
            kind=EventKind.AGENT_FINISHED,
            role="Developer",
            card=number,
            summary=f"PR #{outcome.pr}",
        )
    )
    return outcome


def _pr_body(card: Card, implementation: Implementation, outcome: DeliveryOutcome) -> str:
    lines = [
        implementation.summary,
        "",
        "## Verification",
        "",
        "Lint and the full test suite pass in an isolated worktree.",
        "",
        f"- attempts: {outcome.attempts + 1}",
        f"- escalated: {'yes' if outcome.escalated else 'no'}",
        "",
        "## Files",
        "",
    ]
    lines += [f"- `{f.path}`" for f in implementation.files]
    lines += ["", f"Closes #{card.number}"]
    return "\n".join(lines)


def deliver(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    rules: ProcessRules,
    policy: EscalationPolicy,
    ledger: EscalationLedger,
    ws: Workspace,
    *,
    sprint: str,
    repo: str,
    default_branch: str = "main",
) -> DeliveryResult:
    """Pull stories into progress up to the WIP limit, and deliver them."""
    result = DeliveryResult()
    cards = board.cards()
    counts = board.counts(cards)

    for card in sprint_stories(cards, sprint):
        verdict = rules.may_move(frm=SPRINT_BACKLOG, to=IN_PROGRESS, counts=counts)
        if not verdict.allowed:
            sink.note(EventKind.NOTE, verdict.reason)
            break

        board.set_status(card.item_id, IN_PROGRESS)
        counts[IN_PROGRESS] = counts.get(IN_PROGRESS, 0) + 1
        sink.emit(
            CrewEvent(
                kind=EventKind.CARD_MOVED,
                role="Developer",
                card=card.number,
                summary=card.title[:60],
                **{"from": SPRINT_BACKLOG, "to": IN_PROGRESS},
            )
        )

        try:
            outcome = deliver_story(
                card,
                board=board,
                issues=issues,
                sink=sink,
                ws=ws,
                policy=policy,
                ledger=ledger,
                sprint=sprint,
                repo=repo,
                default_branch=default_branch,
            )
        except Exception as exc:  # noqa: BLE001
            outcome = DeliveryOutcome(
                card=card.number or 0, blocked_reason=f"{type(exc).__name__}: {exc}"
            )
        finally:
            ws.close()

        if outcome.ok:
            board.set_status(card.item_id, IN_REVIEW)
            counts[IN_PROGRESS] -= 1
            counts[IN_REVIEW] = counts.get(IN_REVIEW, 0) + 1
            issues.comment(
                repo,
                card.number or 0,
                f"Implemented in #{outcome.pr} on `{outcome.branch}`. "
                f"Lint and tests pass." + (" Escalated to finish." if outcome.escalated else ""),
            )
            result.delivered.append(outcome)
        else:
            board.set_status(card.item_id, BLOCKED)
            counts[IN_PROGRESS] -= 1
            issues.add_labels(repo, card.number or 0, ["blocked"])
            issues.comment(
                repo,
                card.number or 0,
                f"**Blocked.** {outcome.blocked_reason}\n\n"
                f"Attempts: {outcome.attempts}. "
                f"{'Escalated.' if outcome.escalated else 'Not escalated.'}",
            )
            sink.emit(
                CrewEvent(
                    kind=EventKind.CARD_BLOCKED,
                    role="Developer",
                    card=card.number,
                    summary=(outcome.blocked_reason or "")[:80],
                )
            )
            result.blocked.append(outcome)
            # A usage limit means come back later, not try the next card.
            if outcome.blocked_reason and "usage limit" in outcome.blocked_reason.lower():
                result.rate_limited = True
                break

    return result
