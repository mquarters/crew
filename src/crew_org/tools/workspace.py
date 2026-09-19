"""Applying an implementation to a worktree, and checking it.

Running the crew's output means executing model-generated code. The guards here
are bounded blast radius, not a sandbox: an isolated worktree, a hard timeout, a
capped output size, and no inherited credentials. Real isolation needs a
container — see docs/ways-of-working.md and the note in `check`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from crew_org.crews.delivery_crew import FileWrite
from crew_org.tools.sandbox import Mode, Sandbox

# A runaway test must not hang the tick.
DEFAULT_TIMEOUT = 300
# What a command produced, kept whole. A head-and-tail slice of 3,000
# characters each was "enough to repair from" only if the defect happened to
# sit at one end: story #9's pytest run had thirteen failures, and the model
# repaired three times from a view with the middle cut out before it blocked —
# with 97% of a 262,144-token window unused.
#
# The ceiling below is a guard against a runaway command, not a prompt budget,
# and it keeps the end: a test run puts its summary and its last failure there.
MAX_FAILURE_REPORT_CHARS = 200_000

# Credentials must not be visible to code the model wrote.
STRIPPED_ENV = (
    "GITHUB_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
)


@dataclass
class CommandResult:
    command: str
    code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


@dataclass
class CheckResult:
    """The verdict on an implementation, and the evidence for it."""

    results: list[CommandResult]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failure_report(self) -> str:
        """What the Developer sees when repairing. Only the failures, whole.

        Bounded once, here, and only against a runaway command — never by the
        old per-command slice, which cut every report whether it needed it or
        not and cut it in the middle, where the failures are.
        """
        parts = []
        for result in self.results:
            if result.ok:
                continue
            reason = "timed out" if result.timed_out else f"exit {result.code}"
            parts.append(f"$ {result.command}\n({reason})\n{result.output}")
        report = "\n\n".join(parts)
        if len(report) > MAX_FAILURE_REPORT_CHARS:
            dropped = len(report) - MAX_FAILURE_REPORT_CHARS
            report = (
                f"… {dropped:,} characters dropped from the start of this report …\n\n"
                + report[-MAX_FAILURE_REPORT_CHARS:]
            )
        return report


def apply_implementation(worktree: Path, implementation) -> list[str]:
    """Write new files and apply edits to existing ones.

    New files and edits are separate on purpose. A new file is written whole;
    an existing file is only ever changed by name, so nothing the model did not
    name can be touched.
    """
    from crew_org.tools.ast_edit import Edit, EditError, apply_edits  # noqa: PLC0415

    root = worktree.resolve()
    written = apply(worktree, implementation.new_files)

    by_path: dict[str, list[Edit]] = {}
    for item in implementation.edits:
        by_path.setdefault(item.path, []).append(
            Edit(operation=item.operation, target=item.target, source=item.source)
        )

    for path, edits in by_path.items():
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"{path!r} resolves outside the worktree")
        if not target.exists():
            raise EditError(
                f"{path!r} does not exist. A file that does not exist yet belongs in "
                "new_files, written in full."
            )
        target.write_text(apply_edits(target.read_text(encoding="utf-8"), edits), encoding="utf-8")
        written.append(path)

    return written


def apply(worktree: Path, files: list[FileWrite]) -> list[str]:
    """Write an implementation into the worktree. Returns the paths written.

    Paths were validated at the schema boundary; this re-checks containment
    because the cost of being wrong is writing outside the repository.
    """
    written: list[str] = []
    root = worktree.resolve()
    for item in files:
        target = (root / item.path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"{item.path!r} resolves outside the worktree")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content, encoding="utf-8")
        written.append(item.path)
    return written


def run(
    worktree: Path,
    command: list[str],
    *,
    sandbox: Sandbox | None = None,
    network: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> CommandResult:
    """Run one command against the worktree.

    With a sandbox in `required` mode the command runs inside a container; the
    host is never a fallback. Credentials are stripped either way, because the
    host path exists only as an explicit opt-in and still must not hand a
    repository token to code the model wrote.
    """
    sandbox = sandbox or Sandbox()
    printable = " ".join(command)

    if sandbox.mode is Mode.REQUIRED:
        blocked = sandbox.unavailable_reason()
        if blocked:
            return CommandResult(command=printable, code=126, output=blocked)
        argv = sandbox.command(worktree, command, network=network)
        cwd = None
    else:
        argv = command
        cwd = worktree

    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command=printable, code=-1, output=f"no output within {timeout}s", timed_out=True
        )
    except FileNotFoundError as exc:
        return CommandResult(command=printable, code=127, output=str(exc))

    output = (completed.stdout + completed.stderr).strip()
    return CommandResult(command=printable, code=completed.returncode, output=output)


# What a developer's editor does on save. Deterministic, so there is no reason
# to spend a model round-trip on any of it.
AUTOFIX = (
    ["uv", "run", "ruff", "check", "--fix-only", "."],
    ["uv", "run", "ruff", "format", "."],
)

VERIFY = (
    ["uv", "run", "ruff", "check", "."],
    ["uv", "run", "pytest", "-q"],
)


def check(
    worktree: Path, *, sandbox: Sandbox | None = None, timeout: int = DEFAULT_TIMEOUT
) -> CheckResult:
    """Format what a formatter owns, then lint and test what is left.

    The autofix pass is not part of the verdict, and deliberately so. An unused
    import and a missed blank line are not judgement calls — they have exactly
    one correct resolution, which a tool applies in milliseconds. Sending them
    to the model instead spends a repair attempt, and the repair attempts are
    the budget reserved for failures that actually need thinking. A card was
    blocked having spent every attempt on an unused variable and a long line,
    never once reaching the question of whether its logic was right.

    So the fixable is fixed, and the model is asked only about the rest. What
    survives a formatter is, by construction, something a formatter could not
    decide.

    Only the dependency step is given a network. Everything after it runs with
    none at all, so generated code cannot reach anything while it executes.
    """
    sandbox = sandbox or Sandbox()
    results = [
        run(
            worktree,
            ["uv", "sync", "--extra", "dev", "--quiet"],
            sandbox=sandbox,
            network=True,
            timeout=timeout,
        )
    ]
    if results[0].ok:
        for command in AUTOFIX:
            # Outcome ignored on purpose: whatever the autofix could not fix is
            # reported by the lint that follows, which is the verdict.
            run(worktree, command, sandbox=sandbox, network=False, timeout=timeout)
        for command in VERIFY:
            results.append(run(worktree, command, sandbox=sandbox, network=False, timeout=timeout))
    return CheckResult(results=results)
