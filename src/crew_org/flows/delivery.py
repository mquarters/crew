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
from crew_org.flows.merge import merge_approved
from crew_org.git_ops import Workspace, branch_name
from crew_org.process import ProcessRules
from crew_org.tools import claude_code, regression, workspace
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
# Source and tests are shown in full on a repair, bounded so a large tree does
# not crowd out the failure itself.
MAX_SOURCE_CHARS = 20_000


@dataclass
class DeliveryOutcome:
    card: int
    branch: str | None = None
    pr: int | None = None
    blocked_reason: str | None = None
    attempts: int = 0
    escalated: bool = False
    diff: str | None = None

    @property
    def ok(self) -> bool:
        return self.pr is not None or self.diff is not None

    @property
    def landed(self) -> bool:
        """Did this actually reach a pull request? False for a dry run."""
        return self.pr is not None


@dataclass
class DeliveryResult:
    delivered: list[DeliveryOutcome] = field(default_factory=list)
    blocked: list[DeliveryOutcome] = field(default_factory=list)
    recovered: list[int] = field(default_factory=list)
    not_ours: list[int] = field(default_factory=list)
    landed: list[int] = field(default_factory=list)
    conflicted: list[int] = field(default_factory=list)
    rate_limited: bool = False


def reconcile_orphans(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    cards: list[Card],
    *,
    repo: str,
) -> list[int]:
    """Return cards stranded In Progress to the backlog.

    A tick can die — killed, crashed, machine slept — and leave a card claimed
    with nobody working it. The board is the state, so the loop heals it on the
    next pass rather than assuming it was left tidy. This is what makes it a
    reconciliation loop rather than a script that must not be interrupted.

    A card with an open pull request is not stranded; it belongs In Review.
    """
    in_progress = [c for c in cards if c.status == IN_PROGRESS and c.work_type == STORY_TYPE]
    if not in_progress:
        return []

    try:
        open_prs = {pull["head"]["ref"]: pull["number"] for pull in issues.open_pulls(repo)}
    except Exception:  # noqa: BLE001
        open_prs = {}

    recovered: list[int] = []
    for card in in_progress:
        number = card.number or 0
        branch = branch_name(number, card.title)
        if branch in open_prs:
            board.set_status(card.item_id, IN_REVIEW)
            sink.emit(
                CrewEvent(
                    kind=EventKind.CARD_MOVED,
                    card=number,
                    summary=f"already has PR #{open_prs[branch]} — moved to review",
                    **{"from": IN_PROGRESS, "to": IN_REVIEW},
                )
            )
            continue

        board.set_status(card.item_id, SPRINT_BACKLOG)
        recovered.append(number)
        sink.emit(
            CrewEvent(
                kind=EventKind.CARD_MOVED,
                card=number,
                summary="stranded In Progress with no PR — returned to the backlog",
                **{"from": IN_PROGRESS, "to": SPRINT_BACKLOG},
            )
        )
    return recovered


def sprint_stories(cards: list[Card], sprint: str, *, repos: set[str] | None = None) -> list[Card]:
    """Stories in this sprint the crew may pick up.

    `repos` is the allow-list of repositories the crew works in. A card outside
    it belongs on the board — refined, prioritised, visible — but is not the
    crew's to implement, so it is never claimed.
    """
    return sorted(
        (
            c
            for c in cards
            if c.status == SPRINT_BACKLOG
            and c.work_type == STORY_TYPE
            and c.state != "CLOSED"
            and (c.sprint == sprint or sprint is None)
            and (repos is None or c.repo in repos)
        ),
        key=lambda c: c.number or 0,
    )


def not_ours(cards: list[Card], sprint: str, repos: set[str]) -> list[Card]:
    """Sprint stories the crew is not permitted to work on. Reported, not hidden."""
    return [
        c
        for c in cards
        if c.status == SPRINT_BACKLOG
        and c.work_type == STORY_TYPE
        and c.state != "CLOSED"
        and (c.sprint == sprint or sprint is None)
        and c.repo not in repos
    ]


def repository_context(worktree: Path, *, include_source: bool = False) -> str:
    """What the repository looks like, so the Developer writes code that fits.

    On a repair, `include_source` shows the current contents of src/ and tests/.
    Without it a repair is asked to fix code it cannot see: it reconstructs from
    scratch, re-plans the module layout, and produces a set of files that
    disagree with each other. Observed on the first real escalation, where two
    repair attempts failed identically with a test importing from the package
    root while the implementation had moved into a submodule.
    """
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

    if include_source:
        lines += ["", "### Current source and tests", ""]
        budget = MAX_SOURCE_CHARS
        for target in sorted(worktree.glob("src/**/*.py")) + sorted(worktree.glob("tests/**/*.py")):
            if ".venv" in target.parts or budget <= 0:
                continue
            body = target.read_text()[:budget]
            budget -= len(body)
            lines += [f"`{target.relative_to(worktree)}`", "", "```python", body.strip(), "```", ""]

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
    dry_run: bool = False,
) -> DeliveryOutcome:
    """Take one story from Sprint Backlog to a pull request.

    A dry run stops once the work is verified: the diff is captured and nothing
    is committed, pushed or opened. It is the same code path as a real run up to
    that point, so what it shows is what would land.
    """
    number = card.number or 0
    outcome = DeliveryOutcome(card=number)
    story_text = f"{card.title}\n\n{issues.get(repo, number).get('body') or ''}"

    branch = branch_name(number, card.title)
    outcome.branch = branch
    worktree = ws.open(branch)
    sink.emit(
        CrewEvent(kind=EventKind.AGENT_STARTED, role="Developer", card=number, summary=branch)
    )

    feedback = ""
    implementation: Implementation | None = None

    while True:
        # Recomputed every pass: a repair must see the files it just wrote, or
        # it is fixing code it cannot read.
        context = repository_context(worktree, include_source=bool(feedback))
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
                    detail={
                        # The reason is the whole diagnostic. Recording only the
                        # disposition says what happened and not why, which is
                        # exactly what a retro needs.
                        "failure_class": FailureClass.SCHEMA,
                        "attempt": outcome.attempts,
                        "reason": decision.reason,
                        "error": str(exc)[:600],
                    },
                )
            )
            if decision.disposition is Disposition.RETRY_LOCAL:
                feedback = f"Your output did not validate:\n{exc}"
                continue
            outcome.blocked_reason = decision.reason
            return outcome

        # Checked before applying: a rewrite that deletes existing public names
        # would otherwise be written to disk and only surface as other stories'
        # tests failing to import, which reads as a coding error rather than as
        # the regression it is.
        regressions = regression.find_regressions(
            worktree, implementation.files, declared=set(implementation.modifies)
        )
        if regressions:
            failure = LocalFailure(
                card=number,
                role="Developer",
                failure_class=FailureClass.VERIFY,
                attempts=outcome.attempts,
                detail=regression.describe(regressions)[:400],
            )
            decision = policy.decide(failure, spent=ledger.spent(sprint))
            outcome.attempts += 1
            sink.emit(
                CrewEvent(
                    kind=EventKind.ESCALATION_DECIDED,
                    role="Developer",
                    card=number,
                    summary=f"REGRESSION — {decision.disposition}",
                    detail={
                        "failure_class": "REGRESSION",
                        "attempt": outcome.attempts,
                        "declared": sorted(implementation.modifies),
                        "removed": {
                            k: sorted(v["removed"]) for k, v in regressions.items() if v["removed"]
                        },
                        "altered": {
                            k: sorted(v["altered"]) for k, v in regressions.items() if v["altered"]
                        },
                    },
                )
            )
            if decision.disposition is Disposition.RETRY_LOCAL:
                feedback = regression.describe(regressions)
                continue
            lost = sorted(
                n for kinds in regressions.values() for names in kinds.values() for n in names
            )
            outcome.blocked_reason = (
                "the implementation kept rewriting existing code it had not declared: "
                + ", ".join(lost)
            )
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
                detail={
                    "failure_class": FailureClass.VERIFY,
                    "attempt": outcome.attempts,
                    "reason": decision.reason,
                    "failing_commands": [r.command for r in check.results if not r.ok],
                    "output": check.failure_report[:600],
                },
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
                # Not an outcome yet — the work is unfinished, not failed.
                ledger.resolve(number, sprint, "parked on a usage limit")
                outcome.blocked_reason = result.detail
                return outcome
            check = workspace.check(worktree)
            if check.ok:
                ledger.resolve(number, sprint, "resolved — lint and tests pass")
                break
            ledger.resolve(number, sprint, "escalated but still failing")
            outcome.blocked_reason = f"escalation did not resolve it: {check.failure_report[:200]}"
            return outcome

        outcome.blocked_reason = decision.reason
        return outcome

    if dry_run:
        # Stop at the point of landing. Everything above this line ran exactly
        # as it would in a real delivery, so the diff is what would have landed.
        outcome.diff = ws.diff()
        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_FINISHED,
                role="Developer",
                card=number,
                summary=f"verified, not landed — {len(implementation.files)} files",
            )
        )
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
    dry_run: bool = False,
    limit: int | None = None,
    repos: set[str] | None = None,
) -> DeliveryResult:
    """Pull stories into progress up to the WIP limit, and deliver them."""
    result = DeliveryResult()
    cards = board.cards()

    # Land first, then branch. A story that branches from a main missing its
    # predecessors is a conflict scheduled for later.
    landed = merge_approved(board, issues, sink, cards=cards, default_repo=repo, repos=repos)
    result.landed = [card for card, _pr in landed.merged]
    result.conflicted = [card for card, _pr in landed.conflicted]
    if landed.merged or landed.conflicted:
        cards = board.cards()

    # Heal before acting: an interrupted run leaves cards claimed by nobody.
    recovered = reconcile_orphans(board, issues, sink, cards, repo=repo)
    if recovered:
        cards = board.cards()
    result.recovered = recovered
    counts = board.counts(cards)

    if repos is not None:
        skipped = not_ours(cards, sprint, repos)
        if skipped:
            result.not_ours = [c.number or 0 for c in skipped]
            sink.note(
                EventKind.NOTE,
                f"{len(skipped)} cards are outside the crew's repositories: "
                + ", ".join(f"#{c.number} ({c.repo})" for c in skipped),
            )

    for index, card in enumerate(sprint_stories(cards, sprint, repos=repos)):
        if limit is not None and index >= limit:
            break
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

        # The card names its own repository. Using a global default would
        # implement a card belonging to one repo inside another, silently.
        card_repo = card.repo or repo
        try:
            outcome = deliver_story(
                card,
                board=board,
                issues=issues,
                sink=sink,
                ws=ws.for_repo(card_repo),
                policy=policy,
                ledger=ledger,
                sprint=sprint,
                repo=card_repo,
                default_branch=default_branch,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            outcome = DeliveryOutcome(
                card=card.number or 0, blocked_reason=f"{type(exc).__name__}: {exc}"
            )
        finally:
            ws.close()

        if outcome.ok and dry_run:
            # Put the card back: a dry run must leave the board as it found it.
            board.set_status(card.item_id, SPRINT_BACKLOG)
            counts[IN_PROGRESS] -= 1
            result.delivered.append(outcome)
            continue

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
        elif dry_run:
            board.set_status(card.item_id, SPRINT_BACKLOG)
            counts[IN_PROGRESS] -= 1
            result.blocked.append(outcome)
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
