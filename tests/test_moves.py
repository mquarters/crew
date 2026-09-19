"""Every board move records who made it."""

from __future__ import annotations

from crew_org.events import EventKind, EventSink
from crew_org.flows.moves import move_card


class FakeBoard:
    def __init__(self):
        self.moves = []
        self.owners = []

    def set_status(self, item_id, column):
        self.moves.append((item_id, column))

    def set_owner_agent(self, item_id, role):
        self.owners.append((item_id, role))


def run(**kwargs):
    board, sink, seen = FakeBoard(), EventSink(None), []
    sink.subscribe(seen.append)
    move_card(board, sink, **kwargs)
    return board, seen


def test_a_move_records_the_role_on_the_card():
    """The board has always had an Owner Agent field and nothing ever wrote it,
    so every card showed that a machine acted and not which role."""
    board, _ = run(item_id="I1", to="Awaiting QA", by="Developer", card=7)

    assert board.moves == [("I1", "Awaiting QA")]
    assert board.owners == [("I1", "Developer")]


def test_a_move_says_where_it_came_from_and_went_to():
    """The `**{"from": ..., "to": ...}` kwargs the old emits passed were dropped
    in silence — CrewEvent ignores extras — so detail was {} on every move."""
    _, seen = run(item_id="I1", to="Done", by="Developer", card=7, frm="Awaiting Approval")

    assert len(seen) == 1
    assert seen[0].detail == {"from": "Awaiting Approval", "to": "Done"}
    assert seen[0].role == "Developer"
    assert seen[0].kind is EventKind.CARD_MOVED


def test_a_move_no_role_made_leaves_the_owner_alone():
    """Orphan reconciliation and parent bookkeeping move cards without any role
    deciding to. Attributing those to a role that did not act is worse than not
    attributing them."""
    board, seen = run(item_id="I1", to="Sprint Backlog", by=None, card=7, frm="In Progress")

    assert board.moves == [("I1", "Sprint Backlog")]
    assert board.owners == [], "no role acted, so nothing is claimed"
    assert seen[0].role is None
    assert seen[0].detail["from"] == "In Progress", "still reported, just not attributed"


def test_a_move_can_report_as_another_kind():
    """A card going to Blocked is a blocking, not a routine move — but it is
    still a move, and it still has to record where it went."""
    _, seen = run(
        item_id="I1",
        to="Blocked",
        by="Developer",
        card=7,
        frm="In Progress",
        kind=EventKind.CARD_BLOCKED,
    )

    assert seen[0].kind is EventKind.CARD_BLOCKED
    assert seen[0].detail == {"from": "In Progress", "to": "Blocked"}
