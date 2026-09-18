"""The retro: what the sprint actually did, and what the process should learn.

The Scrum Master reports; it does not decide. Its one job that needs judgment
is reading the escalation ledger honestly — a high escalation rate is a defect
in task design, and the retro's output is process defects, never a request for
a larger budget.
"""

from __future__ import annotations

from crewai import Crew, Process, Task
from pydantic import BaseModel, Field, field_validator


class ProcessDefect(BaseModel):
    subject: str = Field(description="The story, epic or rule the defect is about")
    problem: str = Field(description="What went wrong, as a fact about this sprint")
    change: str = Field(description="The specific change to how the crew works")

    @field_validator("change")
    @classmethod
    def _is_a_change(cls, value: str) -> str:
        lowered = value.lower()
        if "budget" in lowered and (
            "increase" in lowered or "raise" in lowered or "more" in lowered
        ):
            raise ValueError(
                "a larger escalation budget is not a process improvement. Escalation "
                "rate is a symptom of task design; name the design change instead."
            )
        return value


class Retro(BaseModel):
    summary: str = Field(
        description="What the sprint delivered, plainly, including what went badly"
    )
    defects: list[ProcessDefect] = Field(
        default_factory=list, description="Process changes this sprint argues for"
    )


def write_retro(sprint: str, board_summary: str, escalations: str) -> Retro:
    agents_module = __import__("crew_org.agents", fromlist=["build_agents"])
    agents = agents_module.build_agents("scrum_master")
    task = Task(
        description=(
            f"Write the retro for sprint {sprint}.\n\n"
            f"## What the board says\n\n{board_summary}\n\n"
            f"## The escalation ledger\n\n{escalations or 'No escalations this sprint.'}\n\n"
            "Report what happened, including what went badly, for a Sponsor who was not "
            "present. Cite cards by number.\n"
            "Where escalation was needed, name the specific story that was too large or "
            "whose criteria were ambiguous, and propose the design change. A larger "
            "escalation budget is never the answer."
        ),
        expected_output="A summary and any process defects this sprint argues for.",
        agent=agents["scrum_master"],
        output_pydantic=Retro,
    )
    crew = Crew(
        agents=list(agents.values()), tasks=[task], process=Process.sequential, verbose=False
    )
    return crew.kickoff().pydantic
