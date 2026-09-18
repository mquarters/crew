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


def card(number: int, status: str = INBOX, state: str = "OPEN") -> Card:
    return Card(
        item_id=f"I{number}",
        number=number,
        title=f"Goal {number}",
        status=status,
        state=state,
        repo="sprint-metrics",
    )


class FakeIssues:
    def __init__(self, existing: dict[int, str] | None = None) -> None:
        self.owner = "mqucifer"
        self.posted: list[tuple[int, str]] = []
        self._existing = existing or {}

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

    def cards(self) -> list[Card]:
        return self._cards

    def set_status(self, item_id: str, column: str) -> None:  # pragma: no cover
        self.moves.append((item_id, column))


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


def test_phase_one_never_moves_a_card(monkeypatch):
    board = FakeBoard([card(1)])
    run(board, FakeIssues(), monkeypatch)
    assert board.moves == []


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
