"""Implementation: a story becomes files, and files become a pull request.

The Developer returns a *typed set of file writes* rather than driving a
tool-use loop. That is a deliberate bet on where this model is strong: schema-
constrained output was measured at 5/5 on the substrate gate, whereas a
multi-turn edit loop compounds every tool-call mistake. One structured answer is
easier to validate, repair, and reason about than ten small ones.

Definition of Done is enforced structurally: an implementation with no test is
rejected by the schema, not noticed later by a reviewer.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from crewai import Crew, Process, Task
from pydantic import BaseModel, Field, field_validator, model_validator

from crew_org.agents import build_agents

MAX_FILE_BYTES = 120_000


class FileWrite(BaseModel):
    """One file to create or replace, relative to the repository root."""

    path: str = Field(description="Repository-relative path, e.g. src/pkg/module.py")
    content: str = Field(description="The complete file contents")

    @field_validator("path")
    @classmethod
    def _stays_in_the_repository(cls, value: str) -> str:
        cleaned = value.strip().replace("\\", "/")
        # NOT lstrip("./") — that strips any run of '.' and '/', turning
        # "../../etc/passwd" into "etc/passwd" and defeating the check below.
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if not cleaned:
            raise ValueError("path is empty")
        pure = PurePosixPath(cleaned)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(
                f"{value!r} escapes the repository. Paths must be relative and must not "
                "contain '..'."
            )
        if pure.parts[0] == ".git":
            raise ValueError("refusing to write inside .git")
        return str(pure)

    @field_validator("content")
    @classmethod
    def _is_plausible(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("file content is empty")
        if len(value.encode()) > MAX_FILE_BYTES:
            raise ValueError(
                f"file exceeds {MAX_FILE_BYTES} bytes. Split the work into smaller "
                "changes rather than generating one very large file."
            )
        return value

    @property
    def is_test(self) -> bool:
        name = PurePosixPath(self.path).name
        return name.startswith("test_") or name.endswith("_test.py")


class Implementation(BaseModel):
    summary: str = Field(description="What changed and why, for the pull request body")
    files: list[FileWrite] = Field(description="Every file to create or replace, in full")

    @field_validator("files")
    @classmethod
    def _not_empty(cls, value: list[FileWrite]) -> list[FileWrite]:
        if not value:
            raise ValueError("an implementation must write at least one file")
        paths = [f.path for f in value]
        if len(set(paths)) != len(paths):
            raise ValueError("the same path is written twice; each file appears once, in full")
        return value

    @model_validator(mode="after")
    def _has_a_test(self) -> Implementation:
        """Definition of Done §7.1: every acceptance criterion needs a test.

        Enforced here so a missing test is a SCHEMA failure the repair loop
        handles, rather than something a reviewer catches three columns later.
        """
        if not any(f.is_test for f in self.files):
            raise ValueError(
                "no test file. Every acceptance criterion needs an automated test that "
                "proves it — add a test_*.py that exercises the criteria."
            )
        return self


def implement_story(story: str, *, context: str, feedback: str = "") -> Implementation:
    """Produce the files that satisfy one story.

    `feedback` carries the previous attempt's failure, so a repair sees what
    went wrong instead of starting blind.
    """
    agents = build_agents("developer")
    repair = (
        f"\n\nA previous attempt failed. Fix it.\n\n{feedback}\n\n"
        "Return the complete corrected files, not a patch."
        if feedback
        else ""
    )
    task = Task(
        description=(
            f"Implement this story.\n\n{story}\n\n"
            f"## The repository as it stands\n\n{context}\n\n"
            "Write the test that expresses each acceptance criterion, then the code "
            "that satisfies it. Return every file you create or change, in full — "
            "content is written verbatim, so partial files destroy the original.\n"
            "Match the surrounding code's idiom. Do not widen scope beyond the story."
            f"{repair}"
        ),
        expected_output="A summary and the complete contents of every file to write.",
        agent=agents["developer"],
        output_pydantic=Implementation,
    )
    crew = Crew(
        agents=list(agents.values()), tasks=[task], process=Process.sequential, verbose=False
    )
    return crew.kickoff().pydantic
