"""Sprint close: the increment, the merge, and the retro.

This is the Sponsor's second gate, and it is deliberately the only place
approval is asked for. The crew cannot approve its own pull requests, so
reviewing the increment *is* approving the pull requests that make it up —
one pass at the end of a sprint rather than a decision per story.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crew_org.crews.retro_crew import Retro, write_retro
from crew_org.escalation import EscalationLedger
from crew_org.events import EventKind, EventSink
from crew_org.flows.moves import move_card
from crew_org.git_ops import branch_name
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient

AWAITING_APPROVAL = "Awaiting Approval"
DONE = "Done"
STORY_TYPE = "Story"


@dataclass
class SprintClose:
    sprint: str
    merged: list[int] = field(default_factory=list)
    awaiting_approval: list[tuple[int, int]] = field(default_factory=list)
    unmergeable: list[tuple[int, str]] = field(default_factory=list)
    still_open: list[int] = field(default_factory=list)
    retro: Retro | None = None

    @property
    def complete(self) -> bool:
        return not (self.awaiting_approval or self.still_open or self.unmergeable)


def sprint_cards(cards: list[Card], sprint: str) -> list[Card]:
    return [c for c in cards if c.sprint == sprint and c.work_type == STORY_TYPE]


def board_summary(cards: list[Card], sprint: str) -> str:
    """What the board says, for the Scrum Master to narrate from."""
    lines = []
    for card in sorted(sprint_cards(cards, sprint), key=lambda c: c.number or 0):
        points = int(card.points or 0)
        lines.append(f"#{card.number} [{points}pt] {card.status}: {card.title}")
    return "\n".join(lines) or "No stories in this sprint."


def close_sprint(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    ledger: EscalationLedger,
    *,
    sprint: str,
    repo: str,
    merge: bool = True,
) -> SprintClose:
    """Merge what the Sponsor approved, then report on the sprint."""
    result = SprintClose(sprint=sprint)
    cards = board.cards()

    for card in sorted(sprint_cards(cards, sprint), key=lambda c: c.number or 0):
        number = card.number or 0
        if card.status == DONE:
            continue
        if card.status != AWAITING_APPROVAL:
            result.still_open.append(number)
            continue

        pull = issues.pull_for_branch(repo, branch_name(number, card.title))
        if pull is None:
            result.unmergeable.append((number, "no open pull request"))
            continue

        # The crew cannot approve its own work, so this is where the Sponsor's
        # approval is read rather than requested again.
        reviews = issues.pull_reviews(repo, pull["number"])
        approved = any(r.get("state") == "APPROVED" for r in reviews)
        if not approved:
            result.awaiting_approval.append((number, pull["number"]))
            continue

        if not merge:
            result.awaiting_approval.append((number, pull["number"]))
            continue

        try:
            issues.merge_pull(repo, pull["number"])
        except Exception as exc:  # noqa: BLE001
            result.unmergeable.append((number, str(exc)[:120]))
            continue

        move_card(
            board,
            sink,
            item_id=card.item_id,
            to=DONE,
            by=None,
            card=number,
            frm=AWAITING_APPROVAL,
            summary=f"merged PR #{pull['number']}",
        )
        result.merged.append(number)

    # Report the outcome alongside the failure. Without it an escalation reads
    # as an unresolved failure and the retro concludes the story was shipped
    # broken — which is what happened the first time this ran.
    outcomes = ledger.outcomes(sprint)
    seen: set[int] = set()
    lines = []
    for entry in ledger.entries(sprint):
        if entry.card in seen:
            continue
        seen.add(entry.card)
        ended = outcomes.get(entry.card) or "outcome not recorded"
        lines.append(
            f"#{entry.card} {entry.failure_class} after {entry.local_attempts} local "
            f"attempts — escalation {ended}. Trigger: {entry.detail[:100]}"
        )
    escalations = "\n".join(lines)
    try:
        result.retro = write_retro(sprint, board_summary(board.cards(), sprint), escalations)
    except Exception as exc:  # noqa: BLE001
        sink.note(EventKind.NOTE, f"retro could not be written: {exc}"[:120])

    return result
