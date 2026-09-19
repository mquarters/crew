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

import contextlib
from dataclasses import dataclass, field

from crew_org.columns import INBOX, READY
from crew_org.columns import NEEDS_REFINEMENT as REFINEMENT
from crew_org.config import load_org
from crew_org.crews.refinement_crew import (
    Epic,
    EpicProposal,
    Story,
    StoryProposal,
    propose_epics,
    split_epic,
)
from crew_org.design import DesignPolicy, EpicShape
from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.flows.moves import move_card
from crew_org.git_ops import Workspace
from crew_org.process import ProcessRules
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import Card, ProjectClient
from crew_org.tools.repo_context import repository_context

# Marks a comment as the crew's, so a repeated tick recognises its own work.
# Ticks are reconciliation passes and run repeatedly; without this a goal would
# accrue one identical proposal per tick.
EPIC_PROPOSAL_MARKER = "<!-- crew:epic-proposal -->"

STORY_SPLIT_MARKER = "<!-- crew:story-split -->"

GOAL_TYPE = "Goal"
EPIC_TYPE = "Epic"
STORY_TYPE = "Story"
NEEDS_HUMAN = "needs:human"
NEEDS_DESIGN = "needs:design"


@dataclass
class TickResult:
    """What a tick actually did. The standup is written from this."""

    considered: int = 0
    proposed: list[int] = field(default_factory=list)
    epics_created: list[int] = field(default_factory=list)
    epics_refined: list[int] = field(default_factory=list)
    stories_created: list[int] = field(default_factory=list)
    design_required: list[int] = field(default_factory=list)
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
        move_card(
            board,
            sink,
            item_id=item,
            to=INBOX,
            by="Product Owner",
            card=number,
            summary=f"epic card created — {epic.title[:50]}",
        )
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

    return created


def approved_epics(cards: list[Card]) -> list[Card]:
    """Epics the Sponsor has released from the gate, awaiting story splitting."""
    return [
        c
        for c in cards
        if c.status == REFINEMENT and c.state != "CLOSED" and c.work_type == EPIC_TYPE
    ]


def render_story_body(story: Story, epic_number: int, epic_title: str) -> str:
    """A story issue, written so a test can be derived from it directly."""
    lines = [
        f"As a **{story.as_a}**, I want **{story.i_want}**, so that **{story.so_that}**.",
        "",
        "## Acceptance criteria",
        "",
    ]
    for i, ac in enumerate(story.acceptance_criteria, 1):
        lines += [
            f"{i}. **Given** {ac.given}",
            f"   **When** {ac.when}",
            f"   **Then** {ac.then}",
            "",
        ]
    lines += [
        f"**Estimate** — {story.points} points",
        "",
        "---",
        "",
        f"Split from #{epic_number} *({epic_title})* by the Business Analyst.",
    ]
    return "\n".join(lines)


def render_split(
    epic_title: str, proposal: StoryProposal, decision, numbers: dict[str, int]
) -> str:
    """The record of a split, for whoever reads the epic later."""
    total = sum(s.points for s in proposal.stories)
    lines = [
        STORY_SPLIT_MARKER,
        "## Stories",
        "",
        f"**Business Analyst** split *{epic_title}* into {len(proposal.stories)} stories "
        f"totalling {total} points.",
        "",
    ]
    for story in proposal.stories:
        number = numbers.get(story.title)
        ref = f" — #{number}" if number else ""
        lines.append(f"- **[{story.points}]** {story.title}{ref}")
    lines += [
        "",
        "### Design",
        "",
        ("**Required.** " if decision.required else "**Not required.** ") + decision.reason,
        "",
    ]
    if decision.required:
        lines.append(
            "Labelled `needs:design`. The Architect writes a design note before "
            "implementation begins."
        )
    return "\n".join(lines)


class RepoContext:
    """The repository each card is about, read once per tick and reused.

    Refinement touches several cards across one or two repositories in a pass,
    and the context is identical for all of them — so reading it once is both
    cheaper and what keeps the cached prefix intact between calls.

    A repository that cannot be read is not a reason to stop refining. The
    agent is then working the way it always has, which is worse but not broken,
    and the miss is reported rather than hidden.
    """

    def __init__(self, ws: Workspace | None, sink: EventSink) -> None:
        self._ws = ws
        self._sink = sink
        self._cache: dict[str, str] = {}

    def for_repo(self, repo: str) -> str:
        if self._ws is None:
            return ""
        if repo not in self._cache:
            try:
                clone = self._ws.for_repo(repo).current()
                self._cache[repo] = repository_context(clone, editing=False)
            except Exception as exc:  # noqa: BLE001
                self._sink.note(
                    EventKind.NOTE,
                    f"refining {repo} without its code: {exc}"[:120],
                )
                self._cache[repo] = ""
        return self._cache[repo]


def refine_epics(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    rules: ProcessRules,
    design: DesignPolicy,
    result: TickResult,
    *,
    cards: list[Card],
    default_repo: str,
    context: RepoContext,
) -> None:
    """Split approved epics into stories, and decide whether design is warranted."""
    counts = board.counts(cards)

    for epic_card in approved_epics(cards):
        repo = epic_card.repo or default_repo
        number = epic_card.number
        assert number is not None

        if issues.has_comment_marked(repo, number, STORY_SPLIT_MARKER):
            result.skipped.append((number, "already split"))
            continue

        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_STARTED,
                role="Business Analyst",
                card=number,
                summary=f"split {epic_card.title[:50]}",
            )
        )
        try:
            # The whole epic body. It was cut at 800 characters while the Product
            # Owner one step earlier was given its goal whole — and the Business
            # Analyst is the role that writes the acceptance criteria, so what it
            # cannot see becomes a criterion nobody can satisfy.
            proposal = split_epic(
                epic_card.title,
                _goal_body(issues, repo, number),
                repository=context.for_repo(repo),
            )
        except Exception as exc:  # noqa: BLE001
            result.failed.append((number, f"{type(exc).__name__}: {exc}"))
            sink.emit(
                CrewEvent(
                    kind=EventKind.AGENT_FAILED,
                    role="Business Analyst",
                    card=number,
                    summary=str(exc)[:100],
                )
            )
            continue

        numbers: dict[str, int] = {}
        for story in proposal.stories:
            issue = issues.create(
                repo, story.title, render_story_body(story, number, epic_card.title)
            )
            numbers[story.title] = issue["number"]

            item = board.add_issue(issue["node_id"])
            board.set_select(item, "Work Type", STORY_TYPE)
            board.set_number(item, "Points", story.points)
            if epic_card.priority:
                board.set_select(item, "Priority", epic_card.priority)

            # Ready is a queue, but it still has a limit. A story that cannot
            # enter waits in refinement rather than being dropped.
            verdict = rules.may_move(frm=REFINEMENT, to=READY, counts=counts)
            column = READY if verdict.allowed else REFINEMENT
            move_card(
                board,
                sink,
                item_id=item,
                to=column,
                by="Business Analyst",
                card=issue["number"],
                summary=f"story card created — {story.title[:50]}",
            )
            counts[column] = counts.get(column, 0) + 1
            if not verdict.allowed:
                sink.emit(
                    CrewEvent(
                        kind=EventKind.NOTE,
                        card=issue["number"],
                        summary=f"held in {REFINEMENT}: {verdict.reason}"[:100],
                    )
                )

            # Nesting is best effort; a missing link is not worth losing the story.
            with contextlib.suppress(Exception):
                issues.add_sub_issue(repo, number, issue["id"])
            result.stories_created.append(issue["number"])

        decision = design.decide(
            EpicShape(
                points_total=sum(s.points for s in proposal.stories),
                story_count=len(proposal.stories),
                labels=frozenset(epic_card.labels),
            )
        )
        if decision.required:
            issues.add_labels(repo, number, [NEEDS_DESIGN])
            result.design_required.append(number)

        issues.comment(repo, number, render_split(epic_card.title, proposal, decision, numbers))

        # Moving the card out of the gate *was* the approval. Leaving the label
        # on means the board keeps asking for a decision already made — the same
        # staleness the goal card had.
        issues.remove_label(repo, number, NEEDS_HUMAN)

        result.epics_refined.append(number)
        sink.emit(
            CrewEvent(
                kind=EventKind.AGENT_FINISHED,
                role="Business Analyst",
                card=number,
                summary=f"{len(numbers)} stories"
                + (" — design required" if decision.required else ""),
            )
        )


def tick(
    board: ProjectClient,
    issues: IssueClient,
    sink: EventSink,
    *,
    default_repo: str,
    org: dict | None = None,
    ws: Workspace | None = None,
) -> TickResult:
    """The refinement phase: goals become epics, approved epics become stories.

    Two passes, in dependency order. It stops at Ready because that is where
    refinement ends, not because a Sponsor is waiting: section 1 of the
    constitution names exactly two human gates — epics in `Inbox (Goals)` and
    the sprint review — and admission is neither. `crew_org.flows.loop` runs the
    phase that follows.

    This used to claim sprint admission was "still the Sponsor's decision",
    which invented a third gate the constitution does not have and `crew sprint
    start` explicitly denies: "approving an epic was the scope decision, so
    this is mechanical". Work sat in Ready looking like it needed a human.
    """
    org = org or load_org()
    rules = ProcessRules.from_config(org)
    design = DesignPolicy.from_config(org)

    result = TickResult()
    context = RepoContext(ws, sink)
    sink.note(EventKind.TICK_STARTED, "reading board", tick=1)

    cards = board.cards()
    sink.note(EventKind.NOTE, f"{len(cards)} cards on the board", counts=board.counts(cards))

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
            proposal = propose_epics(
                f"{card.title}\n\n{_goal_body(issues, repo, number)}",
                repository=context.for_repo(repo),
            )
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

    # Epics approved in an earlier tick are refined now. Epics created moments
    # ago are not: they are sitting at the Sponsor's gate, unapproved.
    refine_epics(
        board,
        issues,
        sink,
        rules,
        design,
        result,
        cards=cards,
        default_repo=default_repo,
        context=context,
    )

    sink.note(
        EventKind.TICK_FINISHED,
        f"{len(result.proposed)} goals decomposed, {len(result.epics_created)} epics created, "
        f"{len(result.epics_refined)} epics refined, {len(result.stories_created)} stories, "
        f"{len(result.failed)} failed",
    )
    return result


def _goal_body(issues: IssueClient, repo: str, number: int) -> str:
    """The goal's own text. The title alone is rarely the whole ask."""
    try:
        return (issues.get(repo, number).get("body") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
