"""One tick: take the board as far as it can go.

Refinement always ran to quiescence. Everything after it — admitting a sprint,
reviewing, verifying, merging, delivering — was a separate command invoked by
hand, in the right order, by the Sponsor. Story #8 sat in In Review through a
whole delivery run because nobody had run `crew qa`.

The phases run drain-first: work already started is pushed forward before new
work is claimed, so a story does not branch from a default branch missing its
predecessors. A pass that moves nothing is quiescence, and the tick stops.

A phase that fails does not end the pass. The later phases act on the cards
they can, and the failure is reported beside what did happen — a tick that
aborts on the first error leaves the board in a state nobody chose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from crew_org.escalation import EscalationLedger, EscalationPolicy
from crew_org.events import EventKind, EventSink
from crew_org.git_ops import Workspace
from crew_org.process import ProcessRules
from crew_org.tools.github_issues import IssueClient
from crew_org.tools.github_project import ProjectClient
from crew_org.tools.sandbox import Sandbox

# A pass that keeps moving forever is a bug, not a busy board. Five is well
# past what a real board needs: refinement feeds admission feeds delivery, and
# each is one pass deep.
MAX_PASSES = 5


@dataclass
class Crew:
    """Everything the phases need, built once."""

    board: ProjectClient
    issues: IssueClient
    sink: EventSink
    ws: Workspace
    sandbox: Sandbox
    rules: ProcessRules
    policy: EscalationPolicy
    ledger: EscalationLedger
    org: dict[str, Any]
    repo: str
    repos: set[str]
    sprint: str
    capacity: int
    # Reviewing is done by a second identity: GitHub refuses an approval from
    # the app that opened the pull request.
    reviewer: IssueClient
    reviewer_login: str


@dataclass
class PhaseOutcome:
    name: str
    moved: bool = False
    summary: str = ""
    error: str | None = None
    result: Any = None


@dataclass
class LoopResult:
    passes: int = 0
    outcomes: list[PhaseOutcome] = field(default_factory=list)

    @property
    def failed(self) -> list[PhaseOutcome]:
        return [o for o in self.outcomes if o.error]

    @property
    def moved(self) -> list[PhaseOutcome]:
        return [o for o in self.outcomes if o.moved]

    def last(self, name: str) -> PhaseOutcome | None:
        """The most recent outcome for one phase, which is the one to report."""
        for outcome in reversed(self.outcomes):
            if outcome.name == name:
                return outcome
        return None


def _refine(crew: Crew, *, dry_run: bool) -> PhaseOutcome:
    from crew_org.flows.board_flow import tick as refine  # noqa: PLC0415

    result = refine(
        crew.board, crew.issues, crew.sink, default_repo=crew.repo, org=crew.org, ws=crew.ws
    )
    moved = bool(result.epics_created or result.stories_created)
    return PhaseOutcome(
        "refine",
        moved=moved,
        summary=f"{len(result.epics_created)} epics, {len(result.stories_created)} stories",
        result=result,
    )


def _admit(crew: Crew, *, dry_run: bool) -> PhaseOutcome:
    """Ready to Sprint Backlog.

    Approving the epic was the scope decision, so this is arithmetic. It was
    the one hop `tick` refused to make, on a docstring claiming a third human
    gate that section 1 of the constitution does not have.
    """
    from crew_org.flows.sprint import start_sprint  # noqa: PLC0415

    if dry_run:
        return PhaseOutcome("admit", summary="not admitted — dry run")
    plan = start_sprint(
        crew.board,
        crew.issues,
        crew.sink,
        crew.rules,
        sprint=crew.sprint,
        capacity=crew.capacity,
        default_repo=crew.repo,
    )
    return PhaseOutcome(
        "admit",
        moved=bool(plan.admitted),
        summary=f"{len(plan.admitted)} stories, {plan.points} points",
        result=plan,
    )


def _review(crew: Crew, *, dry_run: bool) -> PhaseOutcome:
    from crew_org.flows.review import review_open_pulls  # noqa: PLC0415

    moved_any = False
    reviewed = skipped = 0
    for repo in sorted(crew.repos):
        result = review_open_pulls(
            crew.reviewer, crew.sink, repo=repo, bot_login=crew.reviewer_login
        )
        reviewed += len(result.reviewed)
        skipped += len(result.skipped)
        moved_any = moved_any or bool(result.reviewed)
    return PhaseOutcome(
        "review", moved=moved_any, summary=f"{reviewed} reviewed, {skipped} already judged"
    )


def _qa(crew: Crew, *, dry_run: bool) -> PhaseOutcome:
    from crew_org.flows.acceptance import close_finished_parents, run_qa  # noqa: PLC0415

    cards = crew.board.cards()
    result = run_qa(
        crew.board, crew.issues, crew.sink, crew.ws, crew.sandbox, cards=cards, repo=crew.repo
    )
    result.parents_closed = close_finished_parents(
        crew.board, crew.issues, crew.sink, crew.board.cards(), repo=crew.repo
    )
    moved = bool(result.verified or result.returned or result.parents_closed)
    return PhaseOutcome(
        "qa",
        moved=moved,
        summary=f"{len(result.verified)} accepted, {len(result.returned)} returned",
        result=result,
    )


def _deliver(crew: Crew, *, dry_run: bool) -> PhaseOutcome:
    """Merge what is approved, then claim what fits.

    `deliver` merges first by design — a story branching from a default branch
    missing its predecessors is a conflict scheduled for later — so landing and
    claiming are one phase rather than two.
    """
    from crew_org.flows.delivery import deliver  # noqa: PLC0415

    result = deliver(
        crew.board,
        crew.issues,
        crew.sink,
        crew.rules,
        crew.policy,
        crew.ledger,
        crew.ws,
        sprint=crew.sprint,
        repo=crew.repo,
        dry_run=dry_run,
        limit=None,
        repos=crew.repos,
    )
    moved = bool(result.landed or result.delivered or result.blocked or result.recovered)
    return PhaseOutcome(
        "deliver",
        moved=moved,
        summary=f"{len(result.landed)} merged, {len(result.delivered)} delivered",
        result=result,
    )


# Drain-first, in dependency order. Refinement produces stories, admission puts
# them in a sprint, and the three that follow push started work forward before
# delivery claims more.
PHASES: tuple[tuple[str, Callable[..., PhaseOutcome]], ...] = (
    ("refine", _refine),
    ("admit", _admit),
    ("review", _review),
    ("qa", _qa),
    ("deliver", _deliver),
)


def run(crew: Crew, *, dry_run: bool = True, max_passes: int = MAX_PASSES) -> LoopResult:
    """Run every phase, in order, until a pass moves nothing."""
    result = LoopResult()

    for _ in range(max_passes):
        result.passes += 1
        moved_this_pass = False

        for name, phase in PHASES:
            try:
                outcome = phase(crew, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                # The pass continues. Later phases act on cards this one never
                # touched, and a tick that aborts here leaves the board partway
                # through a state nobody chose.
                outcome = PhaseOutcome(name, error=f"{type(exc).__name__}: {exc}"[:200])
                crew.sink.note(EventKind.NOTE, f"{name} failed: {outcome.error}"[:120])
            result.outcomes.append(outcome)
            moved_this_pass = moved_this_pass or outcome.moved

        if not moved_this_pass:
            break

    crew.sink.note(
        EventKind.TICK_FINISHED,
        f"{result.passes} pass{'es' if result.passes != 1 else ''}, "
        f"{len(result.moved)} phases moved, {len(result.failed)} failed",
    )
    return result
