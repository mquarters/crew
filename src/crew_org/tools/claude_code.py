"""Escalation to headless Claude Code.

This is the crew's only path to a stronger model, and it runs on the Sponsor's
existing subscription rather than a metered API key. That is the whole point:
hitting a usage limit stops work, it never produces a bill. A rate limit is
therefore an ordinary outcome to be parked and retried, not an error.

Escalation is rationed by crew_org.escalation. Nothing here decides whether to
escalate — only how.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

DEFAULT_TIMEOUT = 1800

# The crew gives Claude Code the tools a developer needs in a worktree and no
# more. It notably cannot push: landing work stays with crew_org.git_ops, which
# refuses to write to main.
ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash(uv run:*),Bash(uv sync:*),Bash(python3:*)"

# Credentials must not be visible to a subprocess writing code. Claude Code
# reads its own OAuth credentials from the user's config, not from here.
STRIPPED_ENV = (
    "GITHUB_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_ID",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
)

# Phrases that mean "come back later", not "this failed".
RATE_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "resets at",
    "too many requests",
)


class Outcome(StrEnum):
    COMPLETED = "completed"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass
class EscalationResult:
    outcome: Outcome
    detail: str
    cost_usd: float | None = None
    turns: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.COMPLETED

    @property
    def should_park(self) -> bool:
        """A rate limit is not a failure — the card waits and the tick ends."""
        return self.outcome is Outcome.RATE_LIMITED


def available(command: str = "claude") -> bool:
    return shutil.which(command) is not None


def _looks_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def escalate(
    worktree: Path,
    prompt: str,
    *,
    command: str = "claude",
    timeout: int = DEFAULT_TIMEOUT,
) -> EscalationResult:
    """Run headless Claude Code against the worktree."""
    if not available(command):
        return EscalationResult(
            outcome=Outcome.UNAVAILABLE,
            detail=f"{command!r} is not on PATH. Escalation needs the Claude Code CLI.",
        )

    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    try:
        completed = subprocess.run(
            [
                command,
                "-p",
                prompt,
                "--output-format",
                "json",
                "--allowedTools",
                ALLOWED_TOOLS,
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return EscalationResult(outcome=Outcome.TIMED_OUT, detail=f"no result within {timeout}s")

    combined = completed.stdout + completed.stderr
    if _looks_rate_limited(combined):
        return EscalationResult(
            outcome=Outcome.RATE_LIMITED,
            detail="subscription usage limit reached; the card waits for the next tick",
        )
    if completed.returncode != 0:
        return EscalationResult(
            outcome=Outcome.FAILED, detail=combined.strip()[:400] or f"exit {completed.returncode}"
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        # A completed run with unparseable output still did the work; the
        # worktree is the real result, so this is not a failure.
        return EscalationResult(outcome=Outcome.COMPLETED, detail=completed.stdout.strip()[:400])

    if payload.get("is_error"):
        detail = str(payload.get("result", ""))[:400]
        outcome = Outcome.RATE_LIMITED if _looks_rate_limited(detail) else Outcome.FAILED
        return EscalationResult(outcome=outcome, detail=detail)

    return EscalationResult(
        outcome=Outcome.COMPLETED,
        detail=str(payload.get("result", ""))[:600],
        cost_usd=payload.get("total_cost_usd"),
        turns=payload.get("num_turns"),
    )
