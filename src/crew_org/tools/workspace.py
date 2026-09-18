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
# Enough failure output to repair from, not so much that it floods the prompt.
MAX_OUTPUT_CHARS = 6000

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
        """What the Developer sees when repairing. Only the failures."""
        parts = []
        for result in self.results:
            if result.ok:
                continue
            reason = "timed out" if result.timed_out else f"exit {result.code}"
            parts.append(f"$ {result.command}\n({reason})\n{result.output}")
        return "\n\n".join(parts)


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
    if len(output) > MAX_OUTPUT_CHARS:
        half = MAX_OUTPUT_CHARS // 2
        elided = len(output) - MAX_OUTPUT_CHARS
        output = f"{output[:half]}\n\n… {elided} characters elided …\n\n{output[-half:]}"
    return CommandResult(command=printable, code=completed.returncode, output=output)


def check(
    worktree: Path, *, sandbox: Sandbox | None = None, timeout: int = DEFAULT_TIMEOUT
) -> CheckResult:
    """Resolve dependencies, lint, and test.

    Only the dependency step is given a network. Lint and tests run with none at
    all, so generated code cannot reach anything while it executes.
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
        for command in (["uv", "run", "ruff", "check", "."], ["uv", "run", "pytest", "-q"]):
            results.append(run(worktree, command, sandbox=sandbox, network=False, timeout=timeout))
    return CheckResult(results=results)
