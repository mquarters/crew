"""Git operations available to agents.

Every write an agent makes to a repository passes through here, which makes this
the one place the "never push to main" rail can be enforced in code.

Branch protection on GitHub is the outer rail: required review, required
`tests` check, no force-push, no deletion. This guard is the inner one. It
matters because protection only rejects a push *after* an agent has decided to
make it — the failure then arrives as a confusing tool error mid-task, rather
than as a clear refusal at the point of the mistake.

It refuses rather than warns, and has deliberately no override flag.
"""

from __future__ import annotations

import re

# Branches an agent may never write to directly, under any circumstances.
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "release", "develop"})

BRANCH_TYPES = frozenset({"feat", "fix", "chore", "docs", "test", "refactor", "spike"})

# <type>/<issue>-<kebab-summary>, per the constitution §8.
BRANCH_PATTERN = re.compile(
    r"^(?P<type>[a-z]+)/(?P<issue>\d+)-(?P<summary>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)


class ProtectedBranchError(RuntimeError):
    """Raised when an agent tries to write to a protected branch."""


class BranchNameError(ValueError):
    """Raised when a branch name does not follow the convention."""


def assert_writable(branch: str) -> None:
    """Refuse a write to a protected branch.

    Raises rather than returning a flag: a caller cannot ignore this by
    accident, and there is deliberately no force parameter.
    """
    if branch.strip().lower() in PROTECTED_BRANCHES:
        raise ProtectedBranchError(
            f"{branch!r} is protected. Agents never push to it — open a pull request "
            "from a branch named for the issue instead. See ways-of-working.md §8."
        )


def branch_name(issue: int, summary: str, *, kind: str = "feat") -> str:
    """Build a conventional branch name for an issue."""
    if kind not in BRANCH_TYPES:
        raise BranchNameError(f"{kind!r} is not one of: {', '.join(sorted(BRANCH_TYPES))}")
    if issue <= 0:
        raise BranchNameError(f"issue number must be positive, got {issue}")

    slug = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")
    if not slug:
        raise BranchNameError(f"summary {summary!r} produced an empty slug")

    name = f"{kind}/{issue}-{slug}"
    assert_writable(name)  # a summary can't smuggle a protected name through
    return name


def validate_branch_name(branch: str) -> None:
    """Check a branch follows the convention, and is not protected."""
    assert_writable(branch)
    match = BRANCH_PATTERN.match(branch)
    if not match:
        raise BranchNameError(
            f"{branch!r} does not match <type>/<issue>-<summary>, e.g. 'feat/42-cycle-time'."
        )
    if match.group("type") not in BRANCH_TYPES:
        raise BranchNameError(
            f"{match.group('type')!r} is not one of: {', '.join(sorted(BRANCH_TYPES))}"
        )
