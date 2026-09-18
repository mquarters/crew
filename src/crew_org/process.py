"""Deterministic process rules.

These were originally a Scrum Master agent's judgment calls. They are code
because they are not judgment: a WIP limit that an LLM can decide to ignore is
not a limit, and card movement that depends on a model's mood is the
"unorchestrated" failure this project exists to remove.

The Scrum Master agent retains only narration — the standup and the retro.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class Refusal(StrEnum):
    UNKNOWN_COLUMN = "unknown_column"
    HUMAN_GATE = "human_gate"
    SKIPS_COLUMNS = "skips_columns"
    WIP_LIMIT = "wip_limit"


class MoveVerdict(BaseModel):
    allowed: bool
    refusal: Refusal | None = None
    reason: str = ""


class ProcessRules:
    """Legal card movement and WIP enforcement, derived from org.yaml."""

    def __init__(
        self,
        *,
        columns: list[str],
        blocked_column: str,
        human_gates: set[str],
        wip_limits: dict[str, int],
        blocked_aging_days: int,
    ) -> None:
        self.columns = columns
        self.blocked_column = blocked_column
        self.human_gates = human_gates
        self.wip_limits = wip_limits
        self.blocked_aging_days = blocked_aging_days
        self._index = {c: i for i, c in enumerate(columns)}

    @classmethod
    def from_config(cls, org: dict[str, Any]) -> ProcessRules:
        board = org["board"]
        return cls(
            columns=board["columns"],
            blocked_column=board["blocked_column"],
            human_gates=set(board["human_gates"]),
            wip_limits=org["wip_limits"],
            blocked_aging_days=org["sprint"]["blocked_aging_days"],
        )

    def known(self, column: str) -> bool:
        return column in self._index or column == self.blocked_column

    def may_move(
        self, *, frm: str, to: str, counts: dict[str, int], by_agent: bool = True
    ) -> MoveVerdict:
        """Decide whether a card may move from `frm` to `to`.

        `counts` is the current occupancy of each column, excluding this card.
        """
        if not self.known(frm) or not self.known(to):
            return MoveVerdict(
                allowed=False,
                refusal=Refusal.UNKNOWN_COLUMN,
                reason=f"{frm!r} -> {to!r}: not a column on this board.",
            )

        # A human gate is the Sponsor's to release. Nothing else may move a
        # card out of one — this is the whole mechanism of the gate.
        if by_agent and frm in self.human_gates and frm != to:
            return MoveVerdict(
                allowed=False,
                refusal=Refusal.HUMAN_GATE,
                reason=f"{frm!r} is a human gate; only the Sponsor may move a card out of it.",
            )

        # Blocked is off-flow: reachable from anywhere, and leaving it returns
        # the card to the normal flow wherever it belongs.
        if to == self.blocked_column or frm == self.blocked_column:
            return self._wip_check(to, counts)

        step = self._index[to] - self._index[frm]
        if step > 1:
            skipped = ", ".join(self.columns[self._index[frm] + 1 : self._index[to]])
            return MoveVerdict(
                allowed=False,
                refusal=Refusal.SKIPS_COLUMNS,
                reason=f"{frm!r} -> {to!r} skips {skipped}. Work does not jump a gate.",
            )

        # step <= 0 is rework — always legal, subject to WIP.
        return self._wip_check(to, counts)

    def _wip_check(self, to: str, counts: dict[str, int]) -> MoveVerdict:
        limit = self.wip_limits.get(to)
        if limit is not None and counts.get(to, 0) >= limit:
            return MoveVerdict(
                allowed=False,
                refusal=Refusal.WIP_LIMIT,
                reason=(
                    f"{to!r} is at its WIP limit ({counts.get(to, 0)}/{limit}). "
                    "Finish the oldest card there before starting more."
                ),
            )
        return MoveVerdict(allowed=True)

    def over_limit(self, counts: dict[str, int]) -> dict[str, tuple[int, int]]:
        """Columns currently exceeding their limit, as {column: (count, limit)}."""
        return {
            column: (counts.get(column, 0), limit)
            for column, limit in self.wip_limits.items()
            if counts.get(column, 0) > limit
        }

    def aging_blocked(self, blocked_since: dict[int, datetime], *, now: datetime) -> dict[int, int]:
        """Blocked cards older than the threshold, as {card: days blocked}."""
        cutoff = timedelta(days=self.blocked_aging_days)
        return {
            card: (now - since).days
            for card, since in blocked_since.items()
            if now - since >= cutoff
        }
