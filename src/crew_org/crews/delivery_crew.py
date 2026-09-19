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
from crew_org.tools.ast_edit import Operation

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


class FileEdit(BaseModel):
    """One change to an existing file, addressed by name rather than position."""

    path: str = Field(description="Repository-relative path of the file to edit")
    operation: Operation = Field(
        description=(
            "replace (an existing definition), add (a new top-level definition), "
            "add_method (to an existing class), add_import, or delete"
        )
    )
    target: str = Field(
        description=(
            "Qualified name: `calculate_throughput`, or `Card.is_completed` for a "
            "method. For add_import, any short label."
        )
    )
    source: str = Field(
        default="",
        description="The complete new definition, or the import statement. Empty only for delete.",
    )

    @field_validator("path")
    @classmethod
    def _stays_in_the_repository(cls, value: str) -> str:
        return FileWrite._stays_in_the_repository(value)

    @model_validator(mode="after")
    def _source_matches_the_operation(self) -> FileEdit:
        if self.operation is not Operation.DELETE and not self.source.strip():
            raise ValueError(f"{self.operation} needs source. Only delete may omit it.")
        return self

    @property
    def is_test(self) -> bool:
        name = PurePosixPath(self.path).name
        return name.startswith("test_") or name.endswith("_test.py")


class Implementation(BaseModel):
    summary: str = Field(description="What changed and why, for the pull request body")
    new_files: list[FileWrite] = Field(
        default_factory=list,
        description="Files that do not exist yet, in full. Never an existing file.",
    )
    edits: list[FileEdit] = Field(
        default_factory=list,
        description="Changes to files that already exist, one per definition.",
    )

    @model_validator(mode="after")
    def _does_something(self) -> Implementation:
        if not self.new_files and not self.edits:
            raise ValueError("an implementation must create a file or edit one")
        return self


class FirstAttempt(Implementation):
    """An implementation written from the story, with nothing yet on disk.

    The test requirement lives here rather than on `Implementation` because a
    repair cannot satisfy it and should not have to. A repair is told to return
    only the edits that fix the failure, and the test its first attempt wrote is
    already in the worktree and usually already correct — so demanding a test in
    every submission asks the model to choose which instruction to disobey.
    Story #11 chose correctly, returned the fix alone, and was rejected for it
    twice.

    Nothing is lost by scoping it: QA reads the worktree and will not accept a
    story until it can name the test that proves each criterion, which is a
    claim the schema could never check anyway.
    """

    @model_validator(mode="after")
    def _has_a_test(self) -> FirstAttempt:
        """Definition of Done §7.1: every acceptance criterion needs a test.

        Satisfied by a new test file or by adding to an existing one — a story
        extending a module usually adds cases rather than a whole file.
        """
        if any(f.is_test for f in self.new_files) or any(e.is_test for e in self.edits):
            return self
        raise ValueError(
            "no test. Every acceptance criterion needs an automated test that proves "
            "it — add one to an existing test file, or create a new test_*.py."
        )


# Identical for every story and every repair, so it is the cacheable prefix.
STANDING_INSTRUCTIONS = (
    "Implement one story.\n\n"
    "Write the test that expresses each acceptance criterion, then the code that "
    "satisfies it.\n\n"
    "## How to return your work\n\n"
    "For a file that does not exist yet, return it in `new_files`, in full.\n"
    "For a file that already exists, return `edits` — one per definition, addressed "
    "by name. You never reproduce code you are not changing, and anything you do not "
    "name is left exactly as it is.\n\n"
    "  replace     an existing function, method or constant, by name\n"
    "  add         a new top-level function or class\n"
    "  add_method  a method on an existing class\n"
    "  add_import  an import the new code needs\n"
    "  delete      remove a definition - deliberate, and rarely what a story wants\n\n"
    "Name a method as `Class.method`. Give the complete definition as `source`; "
    "indentation is corrected for you.\n\n"
    "If your code uses a name the file does not already import, add an `add_import` "
    "edit for it. A spliced definition referring to something unimported is the most "
    "common way these edits fail.\n\n"
    "The project's lint rules are in pyproject.toml and are enforced. Write code that "
    "satisfies them rather than code you would then have to fix.\n\n"
    "Match the surrounding code's idiom. Implement only this story - work belonging "
    "to other stories is not yours to add, even when it looks adjacent. Do not change "
    "lint or tool configuration."
)


def implement_story(
    story: str, *, context: str, feedback: str = "", prior: str = ""
) -> Implementation:
    """Produce the files that satisfy one story.

    `feedback` carries the previous attempt's failure, so a repair sees what
    went wrong instead of starting blind.

    `prior` carries what the Code Reviewer and QA said the last time this story
    was delivered — a different thing entirely. A returned card is delivered
    again from a clean worktree (`worktree add -B ... origin/HEAD`), so that
    work is *not* in the files and must not be described as if it were. Without
    it the crew returns a card for a named defect and then implements it again
    knowing nothing about the defect, which is how a story is returned twice for
    the same reason.
    """
    agents = build_agents("developer")
    repair = (
        f"\n\nA previous attempt failed. Fix it.\n\n{feedback}\n\n"
        "## What is already in the repository\n\n"
        "Your previous attempt has ALREADY BEEN WRITTEN. Every definition it "
        "added is in the listing above and is in the files right now. That "
        "changes what you return:\n\n"
        "- Return ONLY the edits that fix the failure. Not the implementation "
        "again — re-sending work that already landed is how a repair fails.\n"
        "- To change something your last attempt added, use `replace`. Using "
        "`add` on a name already in the listing is rejected, and that name is "
        "there because you put it there.\n"
        "- If a definition is already correct, say nothing about it.\n\n"
        "Change what is broken and keep the rest. Do not redesign the module "
        "layout — a layout change that leaves the tests importing from the old "
        "location fails in exactly the same way."
        if feedback
        else ""
    )
    # Ordered stable-first. Prefix caching matches a common prefix, so anything
    # after the first varying block re-tokenises every call. These standing
    # instructions are identical for every story and every repair, so they go
    # first; the story varies per card, the repository per attempt, and the
    # failure most of all. Measured cache hit rate before reordering: 31%.
    # Ordered stable-first for the prefix cache, then by how much each part
    # varies: the story is fixed for this card, what the gates said is fixed for
    # this delivery, the repository changes per attempt, the failure most of all.
    previously = (
        "\n\n## What the gates said last time this story was delivered\n\n"
        f"{prior}\n\n"
        "That work is **not** in the repository below — a returned story is "
        "delivered again from a clean branch. Treat this as what went wrong "
        "before, and do not repeat it.\n"
        if prior
        else ""
    )
    task = Task(
        description=(
            STANDING_INSTRUCTIONS
            + f"\n\n## The story\n\n{story}\n"
            + previously
            + f"\n## The repository as it stands\n\n{context}\n"
            + repair
        ),
        expected_output=(
            "A summary, plus the new files and the named edits that implement the story."
        ),
        agent=agents["developer"],
        output_pydantic=Implementation if feedback else FirstAttempt,
    )
    crew = Crew(
        agents=list(agents.values()), tasks=[task], process=Process.sequential, verbose=False
    )
    return crew.kickoff().pydantic
