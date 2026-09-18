"""A tick is a reconciliation pass that runs repeatedly, so the properties that
matter are: it proposes only what is missing, it never moves a card in Phase 1,
and a failure on one card does not abandon the rest."""

from __future__ import annotations

from crew_org.crews.refinement_crew import Epic, EpicProposal
from crew_org.events import EventKind, EventSink
from crew_org.flows.board_flow import (
    EPIC_PROPOSAL_MARKER,
    INBOX,
    goal_cards,
    render_proposal,
    tick,
)
from crew_org.tools.github_project import Card

PROPOSAL = EpicProposal(
    epics=[
        Epic(title="Report as a table", outcome="Sponsor sees metrics", rationale="most valuable"),
        Epic(title="Emit JSON", outcome="tooling can consume it", rationale="completes formats"),
    ],
    ordering_rationale="Table first because it answers the question immediately.",
)


def card(
    number: int,
    status: str = INBOX,
    state: str = "OPEN",
    work_type: str | None = "Goal",
) -> Card:
    return Card(
        item_id=f"I{number}",
        number=number,
        title=f"Goal {number}",
        status=status,
        state=state,
        work_type=work_type,
        priority="P0",
        repo="sprint-metrics",
    )


class FakeIssues:
    def __init__(self, existing: dict[int, str] | None = None) -> None:
        self.owner = "mqucifer"
        self.posted: list[tuple[int, str]] = []
        self.created: list[dict] = []
        self.nested: list[tuple[int, int]] = []
        self._existing = existing or {}
        self._next = 100

    def create(self, repo: str, title: str, body: str, labels=None) -> dict:
        self._next += 1
        issue = {
            "number": self._next,
            "id": self._next * 1000,
            "node_id": f"N{self._next}",
            "title": title,
            "body": body,
            "labels": labels or [],
        }
        self.created.append(issue)
        return issue

    def add_sub_issue(self, repo: str, parent_number: int, child_id: int) -> None:
        self.nested.append((parent_number, child_id))

    def has_comment_marked(self, repo: str, number: int, marker: str) -> bool:
        return marker in self._existing.get(number, "")

    def comment(self, repo: str, number: int, body: str) -> dict:
        self.posted.append((number, body))
        return {}

    def get(self, repo: str, number: int) -> dict:
        return {"body": "goal body"}


class FakeBoard:
    def __init__(self, cards: list[Card]) -> None:
        self._cards = cards
        self.moves: list[tuple[str, str]] = []
        self.added: list[str] = []
        self.selects: list[tuple[str, str, str]] = []

    def cards(self) -> list[Card]:
        return self._cards

    def add_issue(self, node_id: str) -> str:
        self.added.append(node_id)
        return f"ITEM_{node_id}"

    def set_status(self, item_id: str, column: str) -> None:
        self.moves.append((item_id, column))

    def set_select(self, item_id: str, field: str, option: str) -> None:
        self.selects.append((item_id, field, option))

    @property
    def existing_card_moves(self) -> list[tuple[str, str]]:
        """Moves applied to cards that were already on the board."""
        existing = {c.item_id for c in self._cards}
        return [m for m in self.moves if m[0] in existing]


def run(board, issues, monkeypatch, proposer=lambda goal: PROPOSAL):
    monkeypatch.setattr("crew_org.flows.board_flow.propose_epics", proposer)
    sink = EventSink(None)
    seen = []
    sink.subscribe(seen.append)
    return tick(board, issues, sink, default_repo="sprint-metrics"), seen


# --- selection -----------------------------------------------------------


def test_only_inbox_cards_are_considered():
    cards = [card(1), card(2, status="Ready"), card(3, status="Done")]
    assert [c.number for c in goal_cards(cards)] == [1]


def test_closed_goals_are_ignored():
    assert goal_cards([card(1, state="CLOSED")]) == []


# --- the tick ------------------------------------------------------------


def test_a_fresh_goal_gets_a_proposal(monkeypatch):
    issues = FakeIssues()
    result, _ = run(FakeBoard([card(1)]), issues, monkeypatch)
    assert result.proposed == [1]
    assert len(issues.posted) == 1
    assert EPIC_PROPOSAL_MARKER in issues.posted[0][1]


def test_a_second_tick_does_not_propose_again(monkeypatch):
    """Ticks run repeatedly; without this a goal accrues one proposal per tick."""
    issues = FakeIssues({1: EPIC_PROPOSAL_MARKER + " earlier proposal"})
    result, _ = run(FakeBoard([card(1)]), issues, monkeypatch)
    assert result.proposed == []
    assert result.skipped == [(1, "already proposed")]
    assert issues.posted == []


def test_existing_cards_are_never_moved(monkeypatch):
    """The crew creates epic cards, but must not move work already on the board —
    the goal stays at its human gate until the Sponsor releases it."""
    board = FakeBoard([card(1)])
    run(board, FakeIssues(), monkeypatch)
    assert board.existing_card_moves == []


def test_epics_become_cards_awaiting_the_sponsor(monkeypatch):
    board, issues = FakeBoard([card(1)]), FakeIssues()
    result, _ = run(board, issues, monkeypatch)

    assert len(issues.created) == 2
    assert result.epics_created == [101, 102]
    # Every epic is labelled for the Sponsor and parked at the gate.
    for issue in issues.created:
        assert issue["labels"] == ["needs:human"]
    assert [m[1] for m in board.moves] == [INBOX, INBOX]
    assert ("ITEM_N101", "Work Type", "Epic") in board.selects


def test_epics_inherit_the_goals_priority(monkeypatch):
    board = FakeBoard([card(1)])
    run(board, FakeIssues(), monkeypatch)
    assert ("ITEM_N101", "Priority", "P0") in board.selects


def test_epics_are_nested_under_their_goal(monkeypatch):
    issues = FakeIssues()
    run(FakeBoard([card(1)]), issues, monkeypatch)
    assert issues.nested == [(1, 101000), (1, 102000)]


def test_a_failure_to_nest_does_not_cost_the_epic(monkeypatch):
    """A board that shows hierarchy is better; a missing link is not worth losing
    the card over."""
    issues = FakeIssues()
    issues.add_sub_issue = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no sub-issues"))
    result, _ = run(FakeBoard([card(1)]), issues, monkeypatch)
    assert len(result.epics_created) == 2


def test_epic_cards_are_not_mistaken_for_goals(monkeypatch):
    """Epics await approval in the same column. Decomposing them again would
    recurse the board into nonsense."""
    cards = [card(1), card(2, work_type="Epic"), card(3, work_type=None)]
    assert [c.number for c in goal_cards(cards)] == [1]


def test_one_failing_goal_does_not_abandon_the_others(monkeypatch):
    def flaky(goal: str):
        if "Goal 1" in goal:
            raise RuntimeError("model unavailable")
        return PROPOSAL

    issues = FakeIssues()
    result, _ = run(FakeBoard([card(1), card(2)]), issues, monkeypatch, proposer=flaky)
    assert result.proposed == [2]
    assert result.failed[0][0] == 1
    assert "model unavailable" in result.failed[0][1]


def test_the_tick_emits_events_for_the_live_view(monkeypatch):
    _, seen = run(FakeBoard([card(1)]), FakeIssues(), monkeypatch)
    kinds = [e.kind for e in seen]
    assert EventKind.TICK_STARTED in kinds
    assert EventKind.AGENT_STARTED in kinds
    assert EventKind.AGENT_FINISHED in kinds
    assert EventKind.TICK_FINISHED in kinds


def test_an_empty_board_is_quiescent(monkeypatch):
    result, _ = run(FakeBoard([]), FakeIssues(), monkeypatch)
    assert result.considered == 0 and result.quiescent


# --- the Sponsor-facing artifact -----------------------------------------


def test_the_proposal_reads_as_a_decision_not_a_transcript():
    body = render_proposal("Goal: report performance", PROPOSAL)
    assert "Report as a table" in body and "Emit JSON" in body
    assert "Outcome" in body and "Why" in body
    # It must tell the Sponsor what to do next, and that nothing moved.
    assert "Nothing has been moved" in body
    assert "Inbox (Goals)" in body


def test_the_marker_is_present_so_the_crew_recognises_its_own_work():
    assert render_proposal("g", PROPOSAL).startswith(EPIC_PROPOSAL_MARKER)
