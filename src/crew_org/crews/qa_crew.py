"""QA: does the running code satisfy the acceptance criteria?

The Reviewer judges the diff. QA judges behaviour, and judges it criterion by
criterion — because "the suite passes" is not the same claim as "every
criterion is proven". A test that exists but exercises something adjacent is
the failure this role is here to catch.

Acceptance is all-or-nothing by construction: a verdict cannot accept a story
while any criterion is unproven.
"""

from __future__ import annotations

from crewai import Crew, Process, Task
from pydantic import BaseModel, Field, field_validator, model_validator

from crew_org.agents import build_agents

MIN_EVIDENCE_CHARS = 20


class CriterionVerdict(BaseModel):
    criterion: str = Field(description="The acceptance criterion, quoted")
    proven: bool = Field(
        description="Is this criterion proven by a test that actually exercises it?"
    )
    evidence: str = Field(
        description="Which test proves it, or what is missing. Name the test function."
    )

    @field_validator("evidence")
    @classmethod
    def _is_specific(cls, value: str) -> str:
        if len(value.strip()) < MIN_EVIDENCE_CHARS:
            raise ValueError(
                "name the test that proves this criterion, or say specifically what is "
                "missing. 'Tested' is not evidence."
            )
        return value


class QAVerdict(BaseModel):
    summary: str = Field(description="What was verified and what the result was")
    accepted: bool = Field(description="True only if every criterion is proven")
    criteria: list[CriterionVerdict] = Field(description="One verdict per criterion")

    @field_validator("criteria")
    @classmethod
    def _not_empty(cls, value: list[CriterionVerdict]) -> list[CriterionVerdict]:
        if not value:
            raise ValueError("a story is accepted against its criteria; list them")
        return value

    @model_validator(mode="after")
    def _acceptance_requires_every_criterion(self) -> QAVerdict:
        unproven = [c.criterion for c in self.criteria if not c.proven]
        if self.accepted and unproven:
            raise ValueError(
                f"cannot accept with {len(unproven)} unproven criteria. "
                "Definition of Done requires every criterion proven by a test."
            )
        return self

    @property
    def unproven(self) -> list[CriterionVerdict]:
        return [c for c in self.criteria if not c.proven]


def verify_story(story: str, *, test_output: str, test_code: str) -> QAVerdict:
    """Judge an implementation against its acceptance criteria."""
    agents = build_agents("qa_engineer")
    task = Task(
        description=(
            f"Verify this story against its acceptance criteria.\n\n{story}\n\n"
            f"## The tests that were written\n\n```python\n{test_code[:12000]}\n```\n\n"
            f"## What running the suite produced\n\n```\n{test_output[:4000]}\n```\n\n"
            "For each acceptance criterion, decide whether a test actually exercises it "
            "and name that test. A passing suite is not the same claim as a proven "
            "criterion — a test that exists but checks something adjacent proves nothing. "
            "Report what you found, including what went badly. Do not soften a failure."
        ),
        expected_output="A verdict per acceptance criterion, with evidence.",
        agent=agents["qa_engineer"],
        output_pydantic=QAVerdict,
    )
    crew = Crew(
        agents=list(agents.values()), tasks=[task], process=Process.sequential, verbose=False
    )
    return crew.kickoff().pydantic
