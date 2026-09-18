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

from crew_org.crews.refinement_crew import Epic, EpicProposal, propose_epics
from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient

# Marks a comment as the crew's, so a repeated tick recognises its own work.
# Ticks are reconciliation passes and run repeatedly; without this a goal would
# accrue one identical proposal per tick.
EPIC_PROPOSAL_MARKER = "<!-- crew:epic-proposal -->"

INBOX = "Inbox (Goals)"
GOAL_TYPE = "Goal"
EPIC_TYPE = "Epic"
NEEDS_HUMAN = "needs:human"


@dataclass
class TickResult:
    """What a tick actually did. The standup is written from this."""

    considered: int = 0
    proposed: list[int] = field(default_factory=list)
    epics_created: list[int] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def quiescent(self) -> bool:
        return not self.proposed


def render_epic_body(epic: Epic, goal_number: int, goal_title: str) -> str:
    """An epic issue, written for the Sponsor deciding whether to approve it."""
    return "\n".join(
        [
            f"**Outcome** — {epic.outcome}",
            "",
            f"**Why** — {epic.rationale}",
            "",
            f"**Stands alone because** — {epic.separately_deliverable}",
            "",
            "---",
            "",
            f"Proposed by the Product Owner from #{goal_number} *({goal_title})*.",
            "",
            "**Awaiting Sponsor approval.** Approve by moving this card out of "
            "`Inbox (Goals)` into `Needs Refinement`. Close it to reject.",
        ]
    )


def render_proposal(
    goal_title: str, proposal: EpicProposal, epic_numbers: dict[str, int] | None = None
) -> str:
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
        created = (epic_numbers or {}).get(epic.title)
        heading = f"### {i}. {epic.title}" + (f" — #{created}" if created else "")
        lines += [
            heading,
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
        "Each epic is now a card in `Inbox (Goals)` labelled `needs:human`, nested "
        "under this goal.",
        "",
        "**Approve or reject each epic individually** — move its card to "
        "`Needs Refinement` to approve, or close it to reject. They are separate "
        "decisions; you can take some and not others.",
        "",
        "This goal card stays where it is. It is the parent tracker, not a card to "
        "move, and its `needs:human` label has been cleared now that the decision "
        "sits with the epics.",
    ]
    return "\n".join(lines)


def goal_cards(cards: list[Card]) -> list[Card]:
    """Cards awaiting an epic proposal.

    Filtered by Work Type, not just by column: the epics the crew creates land
    in the same column awaiting approval, and must never be mistaken for goals
    and decomposed again.
    """
    return [
        c for c in cards if c.status == INBOX and c.state != "CLOSED" and c.work_type == GOAL_TYPE
    ]


def create_epic_cards(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    *,
    repo: str,
    goal: Card,
    proposal: EpicProposal,
) -> dict[str, int]:
    """Turn proposed epics into cards awaiting the Sponsor.

    They land in Inbox (Goals) with needs:human, nested under the goal. That is
    the human gate the constitution describes: the Sponsor approves an epic by
    moving its card, which is the only way work leaves that column.
    """
    created: dict[str, int] = {}
    for epic in proposal.epics:
        issue = issues.create(
            repo,
            epic.title,
            render_epic_body(epic, goal.number or 0, goal.title),
            labels=[NEEDS_HUMAN],
        )
        number = issue["number"]
        created[epic.title] = number

        item = board.add_issue(issue["node_id"])
        board.set_status(item, INBOX)
        board.set_select(item, "Work Type", EPIC_TYPE)
        if goal.priority:
            board.set_select(item, "Priority", goal.priority)

        # Nesting is best effort: a board that shows the hierarchy is better,
        # but a missing link must not cost us the epic.
        try:
            issues.add_sub_issue(repo, goal.number or 0, issue["id"])
        except Exception as exc:  # noqa: BLE001
            sink.emit(
                CrewEvent(
                    kind=EventKind.NOTE,
                    card=number,
                    summary=f"could not nest under #{goal.number}: {exc}"[:100],
                )
            )

        sink.emit(
            CrewEvent(
                kind=EventKind.CARD_MOVED,
                role="Product Owner",
                card=number,
                summary=f"epic card created — {epic.title[:50]}",
                **{"to": INBOX},
            )
        )
    return created


def tick(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    *,
    default_repo: str,
) -> TickResult:
    """Run one reconciliation pass.

    Creates epic cards awaiting approval; never moves work into the sprint.
    """
    result = TickResult()
    sink.note(EventKind.TICK_STARTED, "reading board", tick=1)

    cards = board.cards()
    counts: dict[str, int] = {}
    for card in cards:
        if card.status:
            counts[card.status] = counts.get(card.status, 0) + 1
    sink.note(EventKind.NOTE, f"{len(cards)} cards on the board", counts=counts)

    untyped = [c.number for c in cards if c.status == INBOX and not c.work_type]
    if untyped:
        sink.note(
            EventKind.NOTE,
            f"ignoring untyped cards in {INBOX}: {untyped} — set Work Type",
        )

    for card in goal_cards(cards):
        result.considered += 1
        repo = card.repo or default_repo
        number = card.number
        assert number is not None

        if issues.has_comment_marked(repo, number, EPIC_PROPOSAL_MARKER):
            result.skipped.append((number, "already proposed"))
            sink.emit(
                CrewEvent(kind=EventKind.NOTE, card=number, summary="already proposed — skipping")
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

        epic_numbers = create_epic_cards(
            board, issues, sink, repo=repo, goal=card, proposal=proposal
        )
        result.epics_created.extend(epic_numbers.values())

        # The comment is written last, because it is also the idempotency
        # marker: if card creation fails halfway, the next tick retries rather
        # than recording work that did not happen.
        issues.comment(repo, number, render_proposal(card.title, proposal, epic_numbers))

        # The decision has moved to the epics. Leaving needs:human on the goal
        # would show the Sponsor four things demanding attention when only
        # three do, and make the goal look like the card to move.
        issues.remove_label(repo, number, NEEDS_HUMAN)

        result.proposed.append(number)
        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_FINISHED,
                role="Product Owner",
                card=number,
                summary=f"{len(epic_numbers)} epic cards awaiting approval",
            )
        )

    sink.note(
        EventKind.TICK_FINISHED,
        f"{len(result.proposed)} goals decomposed, {len(result.epics_created)} epics created, "
        f"{len(result.skipped)} skipped, {len(result.failed)} failed",
    )
    return result


def _goal_body(issues: IssueClient, repo: str, number: int) -> str:
    """The goal's own text. The title alone is rarely the whole ask."""
    try:
        return (issues.get(repo, number).get("body") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
