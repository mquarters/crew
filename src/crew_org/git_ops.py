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

import base64
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


# --- working copies ------------------------------------------------------

# Absolute, because git subcommands run with cwd set to the clone: a relative
# worktree path would be created inside the clone rather than beside it.
WORK_ROOT = Path("var").resolve()
CLONES = WORK_ROOT / "repos"
WORKTREES = WORK_ROOT / "worktrees"

# git waits forever on a prompt; fail instead.
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never"}


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class BotIdentity:
    """Who the crew's commits are authored by.

    Commit authorship comes from git config, not from the token, so it must be
    set explicitly or the work is attributed to whoever runs the tick.
    """

    login: str
    user_id: int

    @property
    def name(self) -> str:
        return self.login

    @property
    def email(self) -> str:
        return f"{self.user_id}+{self.login}@users.noreply.github.com"

    @property
    def author(self) -> str:
        return f"{self.name} <{self.email}>"


def _auth_header(token: str) -> str:
    """Basic auth for a GitHub App installation token."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


def _run(args: list[str], *, cwd: Path | None = None, token: str | None = None) -> str:
    """Run git, with credentials supplied per-command.

    The token is passed with -c http.extraheader rather than embedded in the
    remote URL, so it is never written into .git/config where it would outlive
    its hour and end up in a backup.
    """
    import os  # noqa: PLC0415

    command = ["git"]
    if token:
        command += ["-c", f"http.extraheader={_auth_header(token)}"]
    command += args

    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **NO_PROMPT},
    )
    if result.returncode != 0:
        # Never echo the command: it carries the credential.
        raise GitError(f"git {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


class Workspace:
    """An isolated worktree for one card.

    Each developer agent works in its own worktree so concurrent cards cannot
    see or stomp each other's changes.
    """

    def __init__(self, owner: str, repo: str, token: str, identity: BotIdentity) -> None:
        self.owner = owner
        self.repo = repo
        self.token = token
        self.identity = identity
        self.clone = CLONES / repo
        self.path: Path | None = None
        self.branch: str | None = None

    def for_repo(self, repo: str) -> Workspace:
        """A workspace for another repository in the same organization.

        Returns self when the repository already matches, so the common case
        costs nothing and callers need not check.
        """
        if repo == self.repo:
            return self
        return Workspace(self.owner, repo, self.token, self.identity)

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"

    def _ensure_clone(self) -> None:
        if (self.clone / ".git").exists():
            _run(["fetch", "origin", "--prune"], cwd=self.clone, token=self.token)
            return
        self.clone.parent.mkdir(parents=True, exist_ok=True)
        _run(["clone", self.url, str(self.clone)], token=self.token)

    def _default_branch(self) -> str:
        """The repo's default branch, asked rather than assumed."""
        try:
            ref = _run(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=self.clone)
            return ref.rsplit("/", 1)[-1]
        except GitError:
            _run(["remote", "set-head", "origin", "--auto"], cwd=self.clone, token=self.token)
            ref = _run(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=self.clone)
            return ref.rsplit("/", 1)[-1]

    def current(self) -> Path:
        """The repository at its default branch, for reading.

        Refinement has no branch of its own — it is deciding what work should
        exist, not doing it — so it reads the clone directly rather than paying
        for a worktree it would only throw away.
        """
        self._ensure_clone()
        default = self._default_branch()
        _run(["checkout", "--force", f"origin/{default}"], cwd=self.clone, token=self.token)
        return self.clone

    def open(self, branch: str) -> Path:
        """Create a clean worktree on a new branch off origin/main."""
        assert_writable(branch)
        validate_branch_name(branch)
        self._ensure_clone()

        path = (WORKTREES / branch.replace("/", "__")).resolve()
        if path.exists():
            self.close(path)

        # Git records worktrees in the clone, and that record outlives the
        # directory: a tree deleted from the filesystem is still "checked out"
        # as far as git is concerned, and the branch cannot be claimed again.
        # Prune reconciles the registry with what is actually on disk.
        _run(["worktree", "prune"], cwd=self.clone)
        path.parent.mkdir(parents=True, exist_ok=True)

        _run(
            ["worktree", "add", "-B", branch, str(path), "origin/HEAD"],
            cwd=self.clone,
            token=self.token,
        )
        self.path, self.branch = path, branch
        return path

    def open_existing(self, branch: str) -> Path:
        """Check out a branch that already exists on the remote, to inspect it."""
        self._ensure_clone()
        _run(["worktree", "prune"], cwd=self.clone)

        path = (WORKTREES / f"review__{branch.replace('/', '__')}").resolve()
        if path.exists():
            self.close(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _run(
            ["worktree", "add", "--detach", str(path), f"origin/{branch}"],
            cwd=self.clone,
            token=self.token,
        )
        self.path, self.branch = path, branch
        return path

    def commit(self, message: str) -> bool:
        """Commit everything in the worktree. False if there was nothing to commit."""
        if self.path is None:
            raise GitError("no worktree open")
        _run(["add", "-A"], cwd=self.path)
        if not _run(["status", "--porcelain"], cwd=self.path):
            return False
        _run(
            [
                "-c",
                f"user.name={self.identity.name}",
                "-c",
                f"user.email={self.identity.email}",
                "commit",
                "-m",
                message,
                "--author",
                self.identity.author,
            ],
            cwd=self.path,
        )
        return True

    def diff(self) -> str:
        """The change as a patch, without committing it.

        Staging is how untracked files become visible to diff; nothing is
        committed, so a dry run leaves the worktree exactly as it found it.
        """
        if self.path is None:
            raise GitError("no worktree open")
        _run(["add", "-A"], cwd=self.path)
        return _run(["diff", "--cached"], cwd=self.path)

    def diff_if_open(self) -> str | None:
        """The patch, or None when there is no worktree left to read.

        Used to keep the evidence from a card that failed, where the caller
        cannot know whether a worktree was ever opened — and where raising
        would replace the failure being recorded with a different one.
        """
        if self.path is None:
            return None
        try:
            return self.diff()
        except GitError:
            return None

    def push(self) -> None:
        if self.path is None or self.branch is None:
            raise GitError("no worktree open")
        assert_writable(self.branch)
        _run(["push", "-u", "origin", self.branch], cwd=self.path, token=self.token)

    def close(self, path: Path | None = None) -> None:
        """Remove the worktree. The branch and its commits survive on the remote."""
        target = path or self.path
        if target is None:
            return
        try:
            _run(["worktree", "remove", "--force", str(target)], cwd=self.clone)
        except GitError:
            shutil.rmtree(target, ignore_errors=True)
        if target == self.path:
            self.path = None

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
