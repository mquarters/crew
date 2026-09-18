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


def run(worktree: Path, command: list[str], *, timeout: int = DEFAULT_TIMEOUT) -> CommandResult:
    """Run one command in the worktree, with credentials stripped."""
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    printable = " ".join(command)
    try:
        completed = subprocess.run(
            command,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command=printable,
            code=-1,
            output=f"no output within {timeout}s",
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return CommandResult(command=printable, code=127, output=str(exc))

    output = (completed.stdout + completed.stderr).strip()
    if len(output) > MAX_OUTPUT_CHARS:
        half = MAX_OUTPUT_CHARS // 2
        elided = len(output) - MAX_OUTPUT_CHARS
        output = f"{output[:half]}\n\n… {elided} characters elided …\n\n{output[-half:]}"
    return CommandResult(command=printable, code=completed.returncode, output=output)


def check(worktree: Path, *, timeout: int = DEFAULT_TIMEOUT) -> CheckResult:
    """Lint and test the worktree.

    NOTE: this executes code the model wrote, on this machine. The worktree
    bounds what it is likely to touch and the timeout bounds how long, but
    neither is a sandbox. Run the crew in a container before pointing it at
    anything you would mind losing.
    """
    results = [
        run(worktree, ["uv", "sync", "--extra", "dev", "--quiet"], timeout=timeout),
    ]
    if results[0].ok:
        results.append(run(worktree, ["uv", "run", "ruff", "check", "."], timeout=timeout))
        results.append(run(worktree, ["uv", "run", "pytest", "-q"], timeout=timeout))
    return CheckResult(results=results)
