"""Refinement: a Sponsor goal becomes epics, and an epic becomes stories.

The constitution's rules for what counts as a well-formed story are expressed
here as validators rather than as prose in a prompt. A story with one acceptance
criterion, or an estimate off the scale, is rejected by the schema — which makes
it a SCHEMA failure the repair loop already handles, instead of something a
reviewer has to notice later.
"""

from __future__ import annotations

from crewai import Crew, Process, Task
from pydantic import BaseModel, Field, field_validator

from crew_org.agents import build_agents

# Modified Fibonacci, per constitution §6. Nothing larger enters a sprint.
POINT_SCALE = (1, 2, 3, 5, 8)
MIN_CRITERIA = 2


class AcceptanceCriterion(BaseModel):
    """One Given/When/Then scenario, per constitution §3."""

    given: str = Field(description="The precondition")
    when: str = Field(description="The action taken")
    then: str = Field(description="The observable outcome a test can assert")


class Story(BaseModel):
    title: str = Field(description="Short imperative title")
    as_a: str = Field(description="The role who wants this")
    i_want: str = Field(description="The capability")
    so_that: str = Field(description="The benefit")
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        description=f"At least {MIN_CRITERIA}; one must cover a failure or edge case"
    )
    points: int = Field(description=f"One of {POINT_SCALE}")

    @field_validator("points")
    @classmethod
    def _on_the_scale(cls, value: int) -> int:
        if value not in POINT_SCALE:
            raise ValueError(
                f"{value} is not on the estimation scale {POINT_SCALE}. "
                "Anything larger than 8 must be split, not admitted."
            )
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _enough_criteria(cls, value: list[AcceptanceCriterion]) -> list[AcceptanceCriterion]:
        if len(value) < MIN_CRITERIA:
            raise ValueError(
                f"a story needs at least {MIN_CRITERIA} acceptance criteria, "
                "at least one of them a failure or edge case"
            )
        return value


class Epic(BaseModel):
    title: str = Field(description="Short imperative title")
    outcome: str = Field(description="The user-visible outcome this delivers")
    rationale: str = Field(description="Why this slice is worth doing, and why now")


class EpicProposal(BaseModel):
    """What the Product Owner proposes for a goal. Awaits Sponsor approval."""

    epics: list[Epic] = Field(description="The smallest set that covers the goal")
    ordering_rationale: str = Field(description="Why this order delivers value soonest")

    @field_validator("epics")
    @classmethod
    def _not_empty(cls, value: list[Epic]) -> list[Epic]:
        if not value:
            raise ValueError("a goal must decompose into at least one epic")
        return value


class StoryProposal(BaseModel):
    """What the Business Analyst proposes for one epic."""

    epic_title: str
    stories: list[Story]

    @field_validator("stories")
    @classmethod
    def _not_empty(cls, value: list[Story]) -> list[Story]:
        if not value:
            raise ValueError("an epic must decompose into at least one story")
        return value


def propose_epics(goal: str) -> EpicProposal:
    """Product Owner only: a goal becomes a set of epics."""
    agents = build_agents("product_owner")
    task = Task(
        description=(
            f"The Product Sponsor has set this goal:\n\n{goal}\n\n"
            "Propose the smallest set of epics that together deliver it. "
            "Decompose by outcome, never by architectural layer. "
            "Order them so the most valuable is deliverable first."
        ),
        expected_output="A set of epics, each with a title, outcome and rationale.",
        agent=agents["product_owner"],
        output_pydantic=EpicProposal,
    )
    crew = Crew(
        agents=list(agents.values()), tasks=[task], process=Process.sequential, verbose=False
    )
    return crew.kickoff().pydantic


def split_epic(epic: Epic) -> StoryProposal:
    """Business Analyst only: an epic becomes INVEST-sized stories."""
    agents = build_agents("business_analyst")
    task = Task(
        description=(
            f"Split this epic into stories.\n\n"
            f"Epic: {epic.title}\nOutcome: {epic.outcome}\nRationale: {epic.rationale}\n\n"
            "Each story must satisfy INVEST and carry acceptance criteria a test can be "
            "written from directly. Split by workflow step, by business rule, or by "
            "happy-path-then-edge-cases — never by layer."
        ),
        expected_output="Stories with acceptance criteria and estimates.",
        agent=agents["business_analyst"],
        output_pydantic=StoryProposal,
    )
    crew = Crew(
        agents=list(agents.values()), tasks=[task], process=Process.sequential, verbose=False
    )
    return crew.kickoff().pydantic
