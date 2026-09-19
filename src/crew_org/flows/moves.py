"""One place where a card moves, so one place records who moved it.

Sixteen call sites wrote a card's Status directly. Seven of them emitted a
`card.moved` event and two of those named a role, so the board said a machine
had acted and the log said little more. The `**{"from": ..., "to": ...}`
kwargs those emits passed were dropped in silence — `CrewEvent` ignores extra
fields — so no move has ever recorded where it came from.

Moving through here makes the three parts inseparable: the status, the acting
role on the card, and one event that says both.
"""

from __future__ import annotations

from typing import Any

from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.tools.github_project import ProjectClient

OWNER_AGENT = "Owner Agent"


def move_card(
    board: ProjectClient,
    sink: EventSink,
    *,
    item_id: str,
    to: str,
    by: str | None,
    card: int | None = None,
    frm: str | None = None,
    summary: str = "",
    kind: EventKind = EventKind.CARD_MOVED,
    **detail: Any,
) -> None:
    """Move a card, attribute it, and report it.

    `by` is the role that caused the move. Pass `None` where no single role
    did — orphan reconciliation healing an interrupted run, a parent closing
    because its children are done. The move still happens and is still
    reported; the card keeps the owner it had, because attributing a move to a
    role that did not make it is worse than not attributing it at all.
    """
    board.set_status(item_id, to)
    if by is not None:
        board.set_owner_agent(item_id, by)
    sink.emit(
        CrewEvent(
            kind=kind,
            role=by,
            card=card,
            summary=summary,
            detail={"from": frm, "to": to, **detail},
        )
    )
