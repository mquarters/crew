"""The reconciliation pass — one tick of the board.

A tick reads the board, decides what is actionable, and acts. It runs to
quiescence rather than one transition at a time: the Sponsor controls when the
process runs, not the hops within it.

Phase 1 is deliberately read-only. The crew proposes epics as issue comments
and moves nothing, so its decomposition can be judged before it is trusted with
the board.

This is plain reconciliation code, not a CrewAI Flow. A Flow earns its place
when routing genuinely branches by card state; for a single linear pass it
would add ceremony that obscures what is happening. It arrives with Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crew_org.crews.refinement_crew import EpicProposal, propose_epics
from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient

# Marks a comment as the crew's, so a repeated tick recognises its own work.
# Ticks are reconciliation passes and run repeatedly; without this a goal would
# accrue one identical proposal per tick.
EPIC_PROPOSAL_MARKER = "<!-- crew:epic-proposal -->"

INBOX = "Inbox (Goals)"


@dataclass
class TickResult:
    """What a tick actually did. The standup is written from this."""

    considered: int = 0
    proposed: list[int] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def quiescent(self) -> bool:
        return not self.proposed


def render_proposal(goal_title: str, proposal: EpicProposal) -> str:
    """Format a proposal for a human reader who was not present.

    The Sponsor reads this on the card and either approves the epics or does
    not, so it states conclusions and the reasoning behind the ordering — it
    does not narrate the crew's deliberation.
    """
    lines = [
        EPIC_PROPOSAL_MARKER,
        "## Proposed epics",
        "",
        f"**Product Owner** decomposed *{goal_title}* into "
        f"{len(proposal.epics)} epic{'s' if len(proposal.epics) != 1 else ''}, "
        "ordered so the most valuable is deliverable first.",
        "",
    ]
    for i, epic in enumerate(proposal.epics, 1):
        lines += [
            f"### {i}. {epic.title}",
            "",
            f"**Outcome** — {epic.outcome}",
            "",
            f"**Why** — {epic.rationale}",
            "",
        ]
    lines += [
        "### Ordering",
        "",
        proposal.ordering_rationale,
        "",
        "---",
        "",
        "*Nothing has been moved on the board.* Approve these by moving this card out "
        "of `Inbox (Goals)`, or comment with the changes you want and the crew will "
        "re-propose on the next tick.",
    ]
    return "\n".join(lines)


def goal_cards(cards: list[Card]) -> list[Card]:
    """Cards awaiting an epic proposal."""
    return [c for c in cards if c.status == INBOX and c.state != "CLOSED"]


def tick(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    *,
    default_repo: str,
) -> TickResult:
    """Run one reconciliation pass. Proposes; moves nothing."""
    result = TickResult()
    sink.note(EventKind.TICK_STARTED, "reading board", tick=1)

    cards = board.cards()
    counts: dict[str, int] = {}
    for card in cards:
        if card.status:
            counts[card.status] = counts.get(card.status, 0) + 1
    sink.note(EventKind.NOTE, f"{len(cards)} cards on the board", counts=counts)

    for card in goal_cards(cards):
        result.considered += 1
        repo = card.repo or default_repo
        number = card.number
        assert number is not None

        if issues.has_comment_marked(repo, number, EPIC_PROPOSAL_MARKER):
            result.skipped.append((number, "already proposed"))
            sink.emit(
                CrewEvent(
                    kind=EventKind.NOTE,
                    card=number,
                    summary="already proposed — skipping",
                )
            )
            continue

        sink.emit(
            CrewEvent(
                kind=EventKind.CARD_CLAIMED,
                role="Product Owner",
                card=number,
                summary=card.title[:80],
            )
        )
        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_STARTED,
                role="Product Owner",
                card=number,
                summary="decompose goal into epics",
            )
        )
        try:
            proposal = propose_epics(f"{card.title}\n\n{_goal_body(issues, repo, number)}")
        except Exception as exc:  # noqa: BLE001
            result.failed.append((number, f"{type(exc).__name__}: {exc}"))
            sink.emit(
                CrewEvent(
                    kind=EventKind.AGENT_FAILED,
                    role="Product Owner",
                    card=number,
                    summary=str(exc)[:100],
                )
            )
            continue

        issues.comment(repo, number, render_proposal(card.title, proposal))
        result.proposed.append(number)
        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_FINISHED,
                role="Product Owner",
                card=number,
                summary=f"proposed {len(proposal.epics)} epics",
            )
        )

    sink.note(
        EventKind.TICK_FINISHED,
        f"{len(result.proposed)} proposed, {len(result.skipped)} skipped, "
        f"{len(result.failed)} failed",
    )
    return result


def _goal_body(issues: IssueClient, repo: str, number: int) -> str:
    """The goal's own text. The title alone is rarely the whole ask."""
    try:
        return (issues.get(repo, number).get("body") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
