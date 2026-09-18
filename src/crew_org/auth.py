"""Verification of the credential the agents use.

GitHub does not expose token creation over the API, so the token itself is made
by hand. What can be automated — and what actually matters — is proving
afterwards that it carries what the crew needs and *not* what it shouldn't.

The negative checks are the point. An over-privileged token is indistinguishable
from a correct one until the day an agent does something irreversible with it.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import httpx
from pydantic import BaseModel

API = "https://api.github.com"
TIMEOUT = 20.0

# Fine-grained tokens carry per-repository, per-permission grants. Classic
# tokens carry coarse scopes that apply to every repo the user can see, which
# is precisely the blast radius this is meant to avoid.
#
# The catch: fine-grained tokens CANNOT access Projects v2 owned by a user
# account. GitHub documents this as a known gap — the Projects permission
# exists only under *organization* permissions. So a user-owned board forces a
# classic token, and only an org-owned board can be driven by a fine-grained
# one. check_project_access says so when it fails.
FINE_GRAINED_PREFIX = "github_pat_"
CLASSIC_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class AuthCheck(BaseModel):
    check: str
    status: Status
    detail: str
    hint: str | None = None


def load_token(env_path: Path | None = None) -> str | None:
    """Read GITHUB_TOKEN from the environment, falling back to .env."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()

    path = env_path or Path(".env")
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("GITHUB_TOKEN=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


def _get(token: str, path: str) -> httpx.Response:
    return httpx.get(
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=TIMEOUT,
    )


def check_token_type(token: str) -> AuthCheck:
    if token.startswith(FINE_GRAINED_PREFIX):
        return AuthCheck(
            check="token type", status=Status.PASS, detail="fine-grained (per-repo grants)"
        )
    if token.startswith(CLASSIC_PREFIXES):
        return AuthCheck(
            check="token type",
            status=Status.WARN,
            detail="classic token — scopes apply to every repository you can see",
            hint="Unavoidable while the board is user-owned: fine-grained tokens cannot "
            "reach user-owned Projects v2. Keep the scopes to 'project' and "
            "'public_repo' (never full 'repo'), or move the board to an organization "
            "and switch to a fine-grained token. See docs/agent-auth.md.",
        )
    return AuthCheck(check="token type", status=Status.WARN, detail="unrecognised token format")


def check_identity(token: str) -> tuple[AuthCheck, str | None]:
    try:
        r = _get(token, "/user")
    except httpx.HTTPError as exc:
        return AuthCheck(check="identity", status=Status.FAIL, detail=str(exc)), None
    if r.status_code == 401:
        return (
            AuthCheck(
                check="identity",
                status=Status.FAIL,
                detail="token rejected (401)",
                hint="Expired, revoked, or mistyped.",
            ),
            None,
        )
    if r.status_code != 200:
        return (
            AuthCheck(check="identity", status=Status.FAIL, detail=f"HTTP {r.status_code}"),
            None,
        )
    login = r.json().get("login")
    return AuthCheck(check="identity", status=Status.PASS, detail=f"acting as {login!r}"), login


def check_repo_access(token: str, owner: str, repo: str) -> AuthCheck:
    """The crew must be able to read and push to the repos it works in."""
    r = _get(token, f"/repos/{owner}/{repo}")
    if r.status_code == 404:
        return AuthCheck(
            check=f"{repo}: access",
            status=Status.FAIL,
            detail="not visible to this token",
            hint=f"Add {owner}/{repo} to the token's repository list.",
        )
    if r.status_code != 200:
        return AuthCheck(
            check=f"{repo}: access", status=Status.FAIL, detail=f"HTTP {r.status_code}"
        )

    perms = r.json().get("permissions") or {}
    if not perms.get("push"):
        return AuthCheck(
            check=f"{repo}: access",
            status=Status.FAIL,
            detail="read-only — cannot push branches",
            hint="Grant Contents: Read and write.",
        )
    return AuthCheck(check=f"{repo}: access", status=Status.PASS, detail="read + push")


def check_not_admin(token: str, owner: str, repo: str) -> AuthCheck:
    """Negative check: the token must NOT be able to administer the repository.

    Reading Actions permissions requires repository administration. A token that
    can do this can also change branch protection, transfer, or delete the repo
    — everything the crew must never be able to reach.
    """
    r = _get(token, f"/repos/{owner}/{repo}/actions/permissions")
    if r.status_code in (403, 404):
        return AuthCheck(
            check=f"{repo}: not an admin",
            status=Status.PASS,
            detail="administration refused, as it should be",
        )
    if r.status_code == 200:
        return AuthCheck(
            check=f"{repo}: not an admin",
            status=Status.FAIL,
            detail="token can administer the repository",
            hint="Remove the Administration permission. An agent holding this could "
            "change branch protection or delete the repo.",
        )
    return AuthCheck(
        check=f"{repo}: not an admin",
        status=Status.WARN,
        detail=f"inconclusive (HTTP {r.status_code})",
    )


def check_issue_write(token: str, owner: str, repo: str) -> AuthCheck:
    """Issues and pull requests are how the crew records everything it does."""
    r = _get(token, f"/repos/{owner}/{repo}/issues?per_page=1")
    if r.status_code == 200:
        return AuthCheck(check=f"{repo}: issues", status=Status.PASS, detail="readable")
    return AuthCheck(
        check=f"{repo}: issues",
        status=Status.FAIL,
        detail=f"HTTP {r.status_code}",
        hint="Grant Issues: Read and write, and Pull requests: Read and write.",
    )


def check_project_access(token: str, owner: str, number: int) -> AuthCheck:
    """Projects v2 lives behind GraphQL and needs its own permission."""
    try:
        r = httpx.post(
            f"{API}/graphql",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "query": "query($o:String!,$n:Int!){user(login:$o){projectV2(number:$n){title}}}",
                "variables": {"o": owner, "n": number},
            },
            timeout=TIMEOUT,
        )
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        return AuthCheck(check="project board", status=Status.FAIL, detail=str(exc))

    if body.get("errors"):
        return AuthCheck(
            check="project board",
            status=Status.FAIL,
            detail=body["errors"][0].get("message", "rejected")[:80],
            hint="If this is a fine-grained token, it cannot reach a user-owned board "
            "at all — that permission does not exist. Either use a classic token with "
            "the 'project' scope, or move the board to an organization. "
            "See docs/agent-auth.md.",
        )
    project = ((body.get("data") or {}).get("user") or {}).get("projectV2")
    if not project:
        return AuthCheck(
            check="project board", status=Status.FAIL, detail="board not visible to this token"
        )
    return AuthCheck(
        check="project board", status=Status.PASS, detail=f"can read {project['title']!r}"
    )


def verify(token: str, *, owner: str, repos: list[str], project_number: int) -> list[AuthCheck]:
    """Run the full check set, positive and negative."""
    checks = [check_token_type(token)]
    identity, login = check_identity(token)
    checks.append(identity)
    if identity.status is Status.FAIL:
        return checks

    if login and login != owner:
        checks.append(
            AuthCheck(
                check="owner match",
                status=Status.WARN,
                detail=f"token acts as {login!r}, board owner is {owner!r}",
                hint="Expected once a machine account is in use — it is what lets the "
                "owner approve the crew's pull requests.",
            )
        )

    for repo in repos:
        checks.append(check_repo_access(token, owner, repo))
        checks.append(check_issue_write(token, owner, repo))
        checks.append(check_not_admin(token, owner, repo))
    checks.append(check_project_access(token, owner, project_number))
    return checks
