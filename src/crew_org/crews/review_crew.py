"""Code review: judging a diff.

The Reviewer judges the *diff* — correctness, reuse, scope — and never asks
"does it work". That is QA's evidence to produce, from the running system. The
two gates answer different questions and collapsing them loses one of them.

A rejection must carry findings. The schema enforces it, because "looks wrong"
is not a review and an author cannot act on it.
"""

from __future__ import annotations

from crewai import Crew, Process, Task
from pydantic import BaseModel, Field, field_validator, model_validator

from crew_org.agents import build_agents

# The whole diff, or no approval. A head slice of 30,000 characters meant the
# Reviewer approved files it had never seen — and its approval now merges, so
# what it cannot see it must not wave through. Past the ceiling the honest
# review is the one a person would give: this is too large to review in one
# pass, split it.
MAX_DIFF_CHARS = 200_000
MIN_ACTION_CHARS = 15


class Finding(BaseModel):
    file: str = Field(description="The file the finding is in")
    concern: str = Field(description="What is wrong, stated as a fact about the diff")
    action: str = Field(description="What to do about it, specifically")

    @field_validator("action")
    @classmethod
    def _is_actionable(cls, value: str) -> str:
        if len(value.strip()) < MIN_ACTION_CHARS:
            raise ValueError(
                "say specifically what to change. A finding an author cannot act on "
                "is not a finding."
            )
        return value


class ReviewVerdict(BaseModel):
    summary: str = Field(description="One paragraph: what this change does and whether it holds")
    approve: bool = Field(description="True to approve, False to request changes")
    findings: list[Finding] = Field(
        default_factory=list, description="Specific, actionable findings"
    )

    @model_validator(mode="after")
    def _a_rejection_must_say_why(self) -> ReviewVerdict:
        if not self.approve and not self.findings:
            raise ValueError(
                "requesting changes with no findings is not a review. Name what is "
                "wrong and what to do, or approve."
            )
        return self

    @property
    def event(self) -> str:
        return "APPROVE" if self.approve else "REQUEST_CHANGES"


def review_diff(title: str, diff: str, *, acceptance_criteria: str = "") -> ReviewVerdict:
    """Review one pull request's diff."""
    if len(diff) > MAX_DIFF_CHARS:
        return ReviewVerdict(
            summary=(
                f"This diff is {len(diff):,} characters, beyond the "
                f"{MAX_DIFF_CHARS:,} a single review pass can hold. Reviewing part of "
                "it and approving the whole is not a review."
            ),
            approve=False,
            findings=[
                Finding(
                    file="(whole change)",
                    concern="The change is too large to be reviewed in one pass.",
                    action=(
                        "Split it into pull requests that can each be read end to end, "
                        "one story's worth of change per pull request."
                    ),
                )
            ],
        )

    criteria = (
        f"\n\n## Acceptance criteria this must satisfy\n\n{acceptance_criteria}"
        if acceptance_criteria
        else ""
    )
    agents = build_agents("code_reviewer")
    task = Task(
        description=(
            f"Review this pull request.\n\n## Title\n\n{title}{criteria}\n\n"
            f"## Diff\n\n```diff\n{diff}\n```\n\n"
            "Review for correctness first, then reuse and simplification. Name the file "
            "for every finding and say what to do about it. Reject scope creep: a diff "
            "doing more than its change is not ready, however good the extra is.\n"
            "Do not ask whether it works at runtime — that is QA's evidence to produce. "
            "Judge the diff.\n"
            "If it is sound, approve it. Approving good work quickly matters as much as "
            "catching bad work."
        ),
        expected_output="A verdict with a summary and any findings.",
        agent=agents["code_reviewer"],
        output_pydantic=ReviewVerdict,
    )
    crew = Crew(
        agents=list(agents.values()), tasks=[task], process=Process.sequential, verbose=False
    )
    return crew.kickoff().pydantic
