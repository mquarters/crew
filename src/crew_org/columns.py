"""The board's columns, and what they mean.

**Columns are queues.** A column names what a card is *waiting for*, not what is
being done to it. A card sits in one for the whole of that phase's work and
leaves when the phase is finished with it — so a card in `QAing` may be waiting
for QA or being verified by it, and the board does not distinguish. That is the
model, chosen deliberately (crew#13); the alternative is a pull system where
each role claims work into a lane of its own, which would make in-flight work
visible and give WIP limits something more precise to cap.

Every phase moves cards out of its own column. A phase that reads the board
without moving anything is invisible to the orchestrator, which is what `crew
review` was: it iterated GitHub's open pull requests, never touched the board,
and so had no column and no WIP limit while the columns either side were capped.

These lived as duplicate string constants in five flow modules. Renaming two of
them took a commit that touched all five and left the board's own automations
pointing at options that no longer existed.
"""

from __future__ import annotations

INBOX = "Inbox (Goals)"
NEEDS_REFINEMENT = "Needs Refinement"
READY = "Ready"
SPRINT_BACKLOG = "Sprint Backlog"
IN_PROGRESS = "In Progress"
REVIEWING = "Reviewing"
QAING = "QAing"
MERGING = "Merging"
DONE = "Done"
BLOCKED = "Blocked"

# The path a story takes. `Blocked` is not on it: a card reaches there from
# anywhere and leaves only when a person has dealt with it.
FLOW = (
    INBOX,
    NEEDS_REFINEMENT,
    READY,
    SPRINT_BACKLOG,
    IN_PROGRESS,
    REVIEWING,
    QAING,
    MERGING,
    DONE,
)

ALL = (*FLOW, BLOCKED)
