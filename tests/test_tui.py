"""The live view's swimlanes show the board, not what moved while watching."""

from __future__ import annotations

from crew_org.columns import BLOCKED, IN_PROGRESS, READY
from crew_org.events import CrewEvent, EventKind
from crew_org.tui import LiveView


def view() -> LiveView:
    return LiveView([READY, IN_PROGRESS, BLOCKED], budget=3)


def test_the_lanes_start_from_the_real_board():
    """`set_board` existed for exactly this and nothing called it, so the lanes
    started at zero and only ever moved by deltas — reading zero across the row
    while 46 cards sat on the board."""
    v = view()
    v.handle(
        CrewEvent(
            kind=EventKind.TICK_STARTED,
            summary="pass 1",
            detail={"counts": {READY: 7, IN_PROGRESS: 2, BLOCKED: 1}},
        )
    )

    assert v.board[READY] == 7
    assert v.board[IN_PROGRESS] == 2
    assert v.blocked == 1


def test_moves_adjust_the_seeded_counts():
    v = view()
    v.handle(CrewEvent(kind=EventKind.TICK_STARTED, detail={"counts": {READY: 7, IN_PROGRESS: 2}}))
    v.handle(CrewEvent(kind=EventKind.CARD_MOVED, detail={"from": READY, "to": IN_PROGRESS}))

    assert v.board[READY] == 6
    assert v.board[IN_PROGRESS] == 3


def test_a_later_pass_reseeds_rather_than_drifting():
    """Anything that moves a card without the crew emitting an event — a
    GitHub Projects workflow, a person — makes deltas drift. Reseeding every
    pass is what makes that self-correcting."""
    v = view()
    v.handle(CrewEvent(kind=EventKind.TICK_STARTED, detail={"counts": {READY: 7}}))
    v.handle(CrewEvent(kind=EventKind.CARD_MOVED, detail={"from": READY, "to": IN_PROGRESS}))
    v.handle(CrewEvent(kind=EventKind.TICK_STARTED, detail={"counts": {READY: 3}}))

    assert v.board[READY] == 3, "the board wins over the running total"


def test_an_event_carrying_no_counts_leaves_the_lanes_alone():
    v = view()
    v.handle(CrewEvent(kind=EventKind.TICK_STARTED, detail={"counts": {READY: 7}}))
    v.handle(CrewEvent(kind=EventKind.NOTE, summary="something happened"))

    assert v.board[READY] == 7


def test_a_column_the_view_does_not_render_is_ignored():
    """The view is built from the configured columns; a count for something
    else must not invent a lane."""
    v = view()
    v.handle(CrewEvent(kind=EventKind.TICK_STARTED, detail={"counts": {"Nonsense": 4, READY: 1}}))

    assert "Nonsense" not in v.board
    assert v.board[READY] == 1
