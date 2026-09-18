"""Landing approved work.

Merging used to happen only at sprint close, which was consistent with
reviewing the increment at the end — but it meant every story branched from a
`main` that was missing its predecessors. Six stories editing one module from
six different starting points produce their conflicts all at once, at the worst
possible moment.

So approval and merge timing are decoupled. The Sponsor's gate is still the
approval; this lands whatever has passed it, and runs before new work is
claimed so a branch always starts from current `main`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.git_ops import branch_name
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient

QA = "QA"
DONE = "Done"
BLOCKED = "Blocked"
STORY_TYPE = "Story"

# GitHub's word for "this branch and main have both changed the same lines".
CONFLICTED = "dirty"


@dataclass
class MergeResult:
    merged: list[tuple[int, int]] = field(default_factory=list)
    conflicted: list[tuple[int, int]] = field(default_factory=list)
    awaiting_approval: list[tuple[int, int]] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)


def ready_to_land(cards: list[Card], repos: set[str] | None = None) -> list[Card]:
    """Stories that have passed QA and are waiting to land."""
    return [
        c
        for c in cards
        if c.status == QA
        and c.work_type == STORY_TYPE
        and c.state != "CLOSED"
        and (repos is None or c.repo in repos)
    ]


def merge_approved(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    *,
    cards: list[Card],
    default_repo: str,
    repos: set[str] | None = None,
) -> MergeResult:
    """Land every story the Sponsor has approved.

    A conflict is not something to retry or work around: it means two changes
    disagree and a person has to decide. The card is blocked and labelled so it
    is visible, rather than left looking merely slow.
    """
    result = MergeResult()

    for card in ready_to_land(cards, repos):
        number = card.number or 0
        repo = card.repo or default_repo

        pull = issues.pull_for_branch(repo, branch_name(number, card.title))
        if pull is None:
            result.failed.append((number, "no open pull request"))
            continue

        detail = issues.pull(repo, pull["number"])
        approved = any(
            r.get("state") == "APPROVED" for r in issues.pull_reviews(repo, pull["number"])
        )
        if not approved:
            result.awaiting_approval.append((number, pull["number"]))
            continue

        if detail.get("mergeable_state") == CONFLICTED or detail.get("mergeable") is False:
            board.set_status(card.item_id, BLOCKED)
            issues.add_labels(repo, number, ["blocked", "needs:human"])
            issues.comment(
                repo,
                number,
                f"**Blocked — merge conflict.** PR #{pull['number']} and `main` have both "
                "changed the same code, so landing it needs a decision the crew should "
                "not make on its own.\n\n"
                "Resolve the conflict on the branch, or close the pull request and let "
                "the story be re-delivered from current `main`.",
            )
            result.conflicted.append((number, pull["number"]))
            sink.emit(
                CrewEvent(
                    kind=EventKind.CARD_BLOCKED,
                    card=number,
                    summary=f"merge conflict on PR #{pull['number']}",
                )
            )
            continue

        try:
            issues.merge_pull(repo, pull["number"])
        except Exception as exc:  # noqa: BLE001
            result.failed.append((number, str(exc)[:120]))
            continue

        board.set_status(card.item_id, DONE)
        result.merged.append((number, pull["number"]))
        sink.emit(
            CrewEvent(
                kind=EventKind.CARD_MOVED,
                card=number,
                summary=f"merged PR #{pull['number']}",
                **{"from": QA, "to": DONE},
            )
        )

    return result
