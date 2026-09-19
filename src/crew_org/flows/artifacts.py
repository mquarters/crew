"""Writing to GitHub, with the role that wrote it.

#22 put the acting role on the *card* — the board's `Owner Agent` field, and an
event carrying the columns. That covers movement. It does not cover what the
crew writes: a comment and a label change go straight to GitHub through
`IssueClient`, which has no notion of who is calling it, so everything the crew
said appeared as `crew[bot]` and a Sponsor reading the audit trail could not
tell refinement from review without reading the body text.

The two halves are not symmetrical, and that is worth knowing rather than
discovering. A comment has a body, so the role goes in it and can be read back.
A label change has nowhere on GitHub to record an actor — the timeline shows the
bot and nothing else — so that half can only ever land in the crew's own event
log. Both are recorded; only one is visible on GitHub.
"""

from __future__ import annotations

from collections.abc import Iterable

from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.tools.github_issues import IssueClient

BY_MARKER = "<!-- crew:by "


def signed(body: str, by: str | None) -> str:
    """The comment, signed by the role that wrote it.

    Visible, because the point is that a Sponsor can tell who spoke without
    reading for clues; and marked, so the crew can read it back.
    """
    if not by:
        return body
    return f"{body}\n\n{BY_MARKER}{by} -->\n— *{by}*"


def role_of(body: str) -> str | None:
    """The role that signed a comment, or None if nothing did."""
    start = body.find(BY_MARKER)
    if start == -1:
        return None
    end = body.find(" -->", start)
    return body[start + len(BY_MARKER) : end].strip() or None if end != -1 else None


def comment(
    issues: IssueClient,
    sink: EventSink,
    *,
    repo: str,
    number: int,
    body: str,
    by: str | None,
) -> None:
    """Write a comment, signed.

    `by=None` where no single role wrote it — a merge, a card closing because
    its children are done. Nothing is claimed rather than a role being guessed,
    which is the ruling #22 made for card moves.
    """
    issues.comment(repo, number, signed(body, by))
    sink.emit(
        CrewEvent(
            kind=EventKind.NOTE,
            role=by,
            card=number,
            summary="commented",
            detail={"artifact": "comment", "repo": repo},
        )
    )


def label(
    issues: IssueClient,
    sink: EventSink,
    *,
    repo: str,
    number: int,
    by: str | None,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
) -> None:
    """Add and remove labels, recording who did it.

    GitHub's timeline attributes a label change to the bot and offers nowhere
    else to put an actor, so the role is recorded in the event log only. That is
    a real limitation of the platform rather than a shortcut.
    """
    add, remove = list(add), list(remove)
    if add:
        issues.add_labels(repo, number, add)
    for name in remove:
        issues.remove_label(repo, number, name)
    if add or remove:
        sink.emit(
            CrewEvent(
                kind=EventKind.NOTE,
                role=by,
                card=number,
                summary="+" + ",".join(add) if add else "-" + ",".join(remove),
                detail={"artifact": "label", "added": add, "removed": remove, "repo": repo},
            )
        )
