"""The review pass: every open pull request gets a verdict.

Deliberately not limited to the crew's own pull requests. A human contributor's
change is reviewed on the same terms — and because the crew acts as a distinct
bot identity, it *can* approve a pull request the Sponsor opened, which is the
same property that lets the Sponsor approve the crew's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crew_org.columns import IN_PROGRESS, QAING, REVIEWING
from crew_org.crews.review_crew import ReviewVerdict, review_diff
from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.flows.moves import move_card
from crew_org.git_ops import branch_name
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient

REVIEW_MARKER = "<!-- crew:review -->"


@dataclass
class ReviewOutcome:
    pr: int
    approved: bool
    findings: int = 0
    skipped: str | None = None
    # What GitHub was actually told, which is not always the verdict reached.
    # Reporting the verdict alone is how a run printed "approved" over a review
    # GitHub had recorded as COMMENTED, and left the merge waiting on an
    # approval nobody knew was missing.
    event: str | None = None


@dataclass
class ReviewResult:
    reviewed: list[ReviewOutcome] = field(default_factory=list)
    skipped: list[ReviewOutcome] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)


def render_review(verdict: ReviewVerdict) -> str:
    lines = [REVIEW_MARKER, verdict.summary, ""]
    if verdict.findings:
        lines += ["## Findings", ""]
        for finding in verdict.findings:
            lines += [f"**`{finding.file}`** — {finding.concern}", f"→ {finding.action}", ""]
    if verdict.approve and not verdict.findings:
        lines.append("No findings.")
    return "\n".join(lines).strip()


def already_reviewed(reviews: list[dict], bot_login: str) -> bool:
    """Has the crew already put a verdict on this revision?"""
    return any(
        (r.get("user") or {}).get("login") == bot_login and REVIEW_MARKER in (r.get("body") or "")
        for r in reviews
    )


def cards_by_branch(cards: list[Card]) -> dict[str, Card]:
    """The cards waiting for review, indexed by the branch carrying their work.

    Review reads pull requests rather than the board, deliberately: a human's
    change is reviewed on the same terms as the crew's, and a human's change has
    no card. So the board is what a verdict is applied *to*, not what is
    iterated — a pull request with no card is still reviewed, and simply moves
    nothing.
    """
    return {
        branch_name(c.number or 0, c.title): c
        for c in cards
        if c.status == REVIEWING and c.state != "CLOSED"
    }


def review_open_pulls(
    issues: IssueClient,
    sink: EventSink,
    *,
    repo: str,
    bot_login: str,
    board: ProjectClient | None = None,
    cards: list[Card] | None = None,
) -> ReviewResult:
    """Review every open pull request that the crew has not yet judged.

    Moves the card a pull request belongs to out of `Reviewing`: on to `QAing`
    when the diff is approved, back to `In Progress` when changes are requested.
    A queue a phase never drains is not a queue, and until this existed review
    was the one phase that read the board without ever touching it — invisible
    to the orchestrator, and uncapped while the columns either side were not.
    """
    result = ReviewResult()
    waiting = cards_by_branch(cards or []) if board is not None else {}

    for pull in issues.open_pulls(repo):
        number = pull["number"]
        author = (pull.get("user") or {}).get("login", "")

        if pull.get("draft"):
            result.skipped.append(ReviewOutcome(pr=number, approved=False, skipped="draft"))
            continue

        if already_reviewed(issues.pull_reviews(repo, number), bot_login):
            result.skipped.append(
                ReviewOutcome(pr=number, approved=False, skipped="already reviewed")
            )
            continue

        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_STARTED,
                role="Code Reviewer",
                card=number,
                summary=f"PR #{number} by {author}: {pull['title'][:40]}",
            )
        )
        try:
            verdict = review_diff(pull["title"], issues.pull_diff(repo, number))
        except Exception as exc:  # noqa: BLE001
            result.failed.append((number, f"{type(exc).__name__}: {exc}"))
            sink.emit(
                CrewEvent(
                    kind=EventKind.AGENT_FAILED,
                    role="Code Reviewer",
                    card=number,
                    summary=str(exc)[:80],
                )
            )
            continue

        # GitHub refuses an approval from the identity that opened the pull
        # request. The crew reviews as a second app for exactly this reason, so
        # this downgrade now only fires where it should: a pull request the
        # reviewing identity opened itself.
        event = "COMMENT" if author == bot_login else verdict.event
        issues.create_review(repo, number, event=event, body=render_review(verdict))

        result.reviewed.append(
            ReviewOutcome(
                pr=number,
                approved=event == "APPROVE",
                findings=len(verdict.findings),
                event=event,
            )
        )
        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_FINISHED,
                role="Code Reviewer",
                card=number,
                summary=f"{event} — {len(verdict.findings)} findings",
            )
        )

        card = waiting.get(pull.get("head", {}).get("ref", ""))
        if card is not None and board is not None:
            # A COMMENT verdict is the crew reviewing its own pull request,
            # which GitHub will not let it approve. That is not a judgement, so
            # the card stays where it is rather than being moved on an opinion
            # nobody was allowed to record.
            if event == "APPROVE":
                move_card(
                    board,
                    sink,
                    item_id=card.item_id,
                    to=QAING,
                    by="Code Reviewer",
                    card=card.number,
                    frm=REVIEWING,
                    summary="diff approved",
                )
            elif event == "REQUEST_CHANGES":
                move_card(
                    board,
                    sink,
                    item_id=card.item_id,
                    to=IN_PROGRESS,
                    by="Code Reviewer",
                    card=card.number,
                    frm=REVIEWING,
                    summary=f"changes requested — {len(verdict.findings)} findings",
                )

    return result
