"""Failure classification and escalation policy.

Escalation exists for genuinely hard problems. It is never the remedy for a
poorly designed task, so the rules live here — in code — rather than in a
prompt an agent could talk itself past.

See docs/ways-of-working.md §9.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

# A justification must actually name what the agent could not do. This floor is
# a blunt instrument against "too hard" one-liners; the reviewer of the ledger
# is the real check.
MIN_JUSTIFICATION_CHARS = 20


class FailureClass(StrEnum):
    """How a local attempt failed. Determines whether escalation is even legal."""

    SCHEMA = "SCHEMA"
    """Output did not parse into the task's expected structure.

    A prompt or schema defect. Never escalates — escalating would hide the bug.
    """

    SCOPE = "SCOPE"
    """The task was too large or ambiguous to act on.

    A refinement defect. Never escalates — escalating would reward bad splitting.
    """

    VERIFY = "VERIFY"
    """Code was produced; tests or lint failed. May escalate after local repair."""

    CAPABILITY = "CAPABILITY"
    """The agent judges the task beyond its reach. May escalate with justification."""

    REGRESSION = "REGRESSION"
    """The implementation would change a contract merged code already depends on.

    Never escalates. A model that cannot stop reshaping interfaces is a
    task-design defect, not a capability gap, and spending the budget to make a
    rewrite land is the opposite of what the budget is for.
    """


class Disposition(StrEnum):
    """What to do about a failure."""

    RETRY_LOCAL = "retry_local"
    RETURN_TO_REFINEMENT = "return_to_refinement"
    FILE_PROMPT_DEFECT = "file_prompt_defect"
    ESCALATE = "escalate"
    BLOCK = "block"


class LocalFailure(BaseModel):
    """A failed local attempt, awaiting disposition."""

    card: int
    role: str
    failure_class: FailureClass
    attempts: int = Field(ge=0, description="Local repair attempts already made")
    detail: str
    justification: str | None = None


class EscalationDecision(BaseModel):
    disposition: Disposition
    reason: str

    @property
    def escalates(self) -> bool:
        return self.disposition is Disposition.ESCALATE


class EscalationRecord(BaseModel):
    """One ledger entry. Written for every escalation, successful or not."""

    at: datetime
    sprint: str
    card: int
    role: str
    failure_class: FailureClass
    local_attempts: int
    justification: str | None
    detail: str
    outcome: str | None = None


class EscalationLedger:
    """Append-only JSONL record of escalations, per sprint.

    The retro reads this. A high rate produces process-defect issues, never a
    larger budget.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: EscalationRecord) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json() + "\n")

    def resolve(self, card: int, sprint: str, outcome: str) -> None:
        """Record how an escalation ended.

        Written as a second append rather than an edit, because the log is
        append-only and the fact of escalating must survive a run that dies
        before it finishes. Without this an escalation reads as an unresolved
        failure forever, and the retro draws the wrong conclusion from it —
        observed on the first real escalation.
        """
        for entry in reversed(self.entries(sprint)):
            if entry.card == card:
                self.record(entry.model_copy(update={"outcome": outcome, "at": utcnow()}))
                return

    def entries(self, sprint: str | None = None) -> list[EscalationRecord]:
        if not self.path.exists():
            return []
        out: list[EscalationRecord] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = EscalationRecord.model_validate(json.loads(line))
                if sprint is None or entry.sprint == sprint:
                    out.append(entry)
        return out

    def outcomes(self, sprint: str) -> dict[int, str | None]:
        """The latest known outcome per escalated card."""
        latest: dict[int, str | None] = {}
        for entry in self.entries(sprint):
            if entry.card not in latest or entry.outcome is not None:
                latest[entry.card] = entry.outcome
        return latest

    def spent(self, sprint: str) -> int:
        """Escalations charged against this sprint's budget.

        Counted per distinct card: retrying an escalated card does not buy a
        second slot, and does not let one pathological card drain the budget.
        """
        return len({e.card for e in self.entries(sprint)})


class EscalationPolicy:
    """Decides the disposition of a local failure.

    Constructed from config/org.yaml. The `never_escalate` set is enforced here
    regardless of what an agent claims about its own failure.
    """

    def __init__(
        self,
        *,
        never_escalate: set[FailureClass],
        require_justification: set[FailureClass],
        local_repair_attempts: int,
        sprint_budget: int,
    ) -> None:
        self.never_escalate = never_escalate
        self.require_justification = require_justification
        self.local_repair_attempts = local_repair_attempts
        self.sprint_budget = sprint_budget

    @classmethod
    def from_config(cls, org: dict) -> EscalationPolicy:
        esc = org["escalation"]
        return cls(
            never_escalate={FailureClass(c) for c in esc["never_escalate"]},
            require_justification={FailureClass(c) for c in esc["require_justification"]},
            local_repair_attempts=org["execution"]["local_repair_attempts"],
            sprint_budget=org["sprint"]["escalation_budget"],
        )

    def decide(self, failure: LocalFailure, *, spent: int) -> EscalationDecision:
        """Return the disposition for `failure`, given escalations already spent."""
        cls_ = failure.failure_class

        # --- Classes that may never escalate ------------------------------
        if cls_ in self.never_escalate:
            if cls_ is FailureClass.REGRESSION:
                if failure.attempts < self.local_repair_attempts:
                    return EscalationDecision(
                        disposition=Disposition.RETRY_LOCAL,
                        reason=f"REGRESSION, attempt {failure.attempts + 1} of "
                        f"{self.local_repair_attempts}; the contracts it must keep are "
                        "named in the feedback.",
                    )
                return EscalationDecision(
                    disposition=Disposition.BLOCK,
                    reason="The implementation keeps rewriting interfaces other work "
                    "depends on, after being told exactly which to preserve. That needs "
                    "a person, not a larger model.",
                )
            if cls_ is FailureClass.SCOPE:
                return EscalationDecision(
                    disposition=Disposition.RETURN_TO_REFINEMENT,
                    reason="SCOPE failures are a refinement defect; the card is "
                    "returned for decomposition rather than escalated.",
                )
            if failure.attempts < self.local_repair_attempts:
                return EscalationDecision(
                    disposition=Disposition.RETRY_LOCAL,
                    reason=f"SCHEMA failure, attempt {failure.attempts + 1} of "
                    f"{self.local_repair_attempts}; retrying with the validation error supplied.",
                )
            return EscalationDecision(
                disposition=Disposition.FILE_PROMPT_DEFECT,
                reason="SCHEMA failure persisted past local repair. This is a prompt "
                "or schema defect and is filed as one, not escalated.",
            )

        # --- A CAPABILITY claim without justification is not a capability claim ---
        if cls_ in self.require_justification and not _justified(failure.justification):
            return EscalationDecision(
                disposition=Disposition.RETURN_TO_REFINEMENT,
                reason="A CAPABILITY claim without a substantive justification is "
                "rejected and treated as SCOPE.",
            )

        # --- VERIFY must exhaust local repair first ------------------------
        if cls_ is FailureClass.VERIFY and failure.attempts < self.local_repair_attempts:
            return EscalationDecision(
                disposition=Disposition.RETRY_LOCAL,
                reason=f"VERIFY failure, attempt {failure.attempts + 1} of "
                f"{self.local_repair_attempts}; repairing locally before escalation.",
            )

        # --- Budget is the last gate --------------------------------------
        if spent >= self.sprint_budget:
            return EscalationDecision(
                disposition=Disposition.BLOCK,
                reason=f"Sprint escalation budget exhausted ({spent}/{self.sprint_budget}). "
                "The card is blocked; the retro addresses the rate as a process defect.",
            )

        return EscalationDecision(
            disposition=Disposition.ESCALATE,
            reason=f"{cls_} failure eligible for escalation "
            f"({spent + 1}/{self.sprint_budget} of sprint budget).",
        )


def _justified(text: str | None) -> bool:
    return bool(text and len(text.strip()) >= MIN_JUSTIFICATION_CHARS)


def utcnow() -> datetime:
    return datetime.now(UTC)
