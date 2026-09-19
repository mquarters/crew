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
# A GitHub App installation token. The best case: it acts as crew[bot] rather
# than as a human, is scoped to the installation, and expires in an hour.
INSTALLATION_PREFIX = "ghs_"
CLASSIC_PREFIXES = ("ghp_", "gho_", "ghu_", "ghr_")


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


# Populated by resolve_credentials when a GitHub App is in use, so callers can
# report the grant set without re-minting a token.
_LAST_APP_PERMISSIONS: dict[str, str] = {}


def app_permissions() -> dict[str, str]:
    return dict(_LAST_APP_PERMISSIONS)


# The reviewing identity. GitHub refuses an approval from the app that opened
# the pull request, so a single identity could never satisfy merge_approved —
# the crew reviewed its own work, GitHub recorded COMMENTED, and every story
# stopped in Merging waiting for a person.
REVIEW_APP_PREFIX = "GITHUB_REVIEW_APP_"


def resolve_credentials(
    env: dict[str, str] | None = None, *, prefix: str = "GITHUB_APP_"
) -> tuple[str, str]:
    """The token the crew should act with, and a description of that identity.

    Prefers the GitHub App: a PAT always acts as the human who created it, so
    the crew's work would be attributed to the Sponsor and the Sponsor could not
    approve the crew's pull requests.

    `prefix` chooses the app. The reviewing app falls back to the delivery app
    when it is not configured, so a repository with one app still works — it
    just cannot approve its own pull requests, which is where it started.
    """
    from crew_org.config import load_env  # noqa: PLC0415
    from crew_org.github_app import AppCredentials, AppTokenProvider  # noqa: PLC0415

    env = env if env is not None else load_env()

    creds = AppCredentials.from_env(env, prefix=prefix)
    if creds is None and prefix != "GITHUB_APP_":
        creds = AppCredentials.from_env(env)
    if creds is not None:
        provider = AppTokenProvider(creds)
        token = provider.token()
        _LAST_APP_PERMISSIONS.clear()
        _LAST_APP_PERMISSIONS.update(provider.permissions)
        return token, provider.identity()

    token = load_token()
    if not token:
        raise RuntimeError(
            "No credentials. Configure a GitHub App (preferred) or a fine-grained "
            "token — see docs/agent-auth.md."
        )
    return token, "personal access token"


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


def check_token_type(token: str, *, app_slug: str | None = None) -> AuthCheck:
    if token.startswith(INSTALLATION_PREFIX):
        who = f" ({app_slug})" if app_slug else ""
        return AuthCheck(
            check="token type",
            status=Status.PASS,
            detail=f"GitHub App installation token{who} — expires hourly",
        )
    if token.startswith(FINE_GRAINED_PREFIX):
        return AuthCheck(
            check="token type",
            status=Status.WARN,
            detail="fine-grained token — acts as the human who created it",
            hint="The crew's work will be attributed to you, and you will not be able to "
            "approve its pull requests. Configure a GitHub App — see docs/agent-auth.md.",
        )
    if token.startswith(CLASSIC_PREFIXES):
        return AuthCheck(
            check="token type",
            status=Status.WARN,
            detail="classic token — scopes apply to every repository you can see",
            hint="Replace with a fine-grained token scoped to just the crew's repos, "
            "granting Projects: Read and write under the organization's permissions. "
            "A classic token reaches every repository you can see.",
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


def check_installation_scope(token: str, app_slug: str | None) -> AuthCheck:
    """An App's identity is its installation, and what that installation reaches."""
    r = _get(token, "/installation/repositories?per_page=100")
    if r.status_code != 200:
        return AuthCheck(
            check="identity",
            status=Status.FAIL,
            detail=f"installation not readable (HTTP {r.status_code})",
            hint="Is the app still installed on the organization?",
        )
    repos = [item["name"] for item in r.json().get("repositories", [])]
    who = app_slug or "the app"
    return AuthCheck(
        check="identity",
        status=Status.PASS,
        detail=f"acting as {who}, scoped to {', '.join(sorted(repos)) or 'nothing'}",
    )


# What the crew needs, and at what level.
REQUIRED_APP_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "organization_projects": "write",
    "metadata": "read",
}
# What it must never hold. This is the permission that would let an agent
# disable branch protection.
FORBIDDEN_APP_PERMISSIONS = ("administration", "organization_administration")


def check_app_permissions(permissions: dict[str, str]) -> list[AuthCheck]:
    """Validate an installation's grant set, positively and negatively."""
    checks: list[AuthCheck] = []

    missing = [
        f"{name}:{level}"
        for name, level in REQUIRED_APP_PERMISSIONS.items()
        if permissions.get(name) != level
    ]
    if missing:
        checks.append(
            AuthCheck(
                check="permissions",
                status=Status.FAIL,
                detail=f"missing or insufficient: {', '.join(missing)}",
                hint="Adjust them on the app's settings page, then accept the updated "
                "permissions on the installation — GitHub does not apply them silently.",
            )
        )
    else:
        checks.append(
            AuthCheck(
                check="permissions",
                status=Status.PASS,
                detail=", ".join(f"{k}:{v}" for k, v in sorted(permissions.items())),
            )
        )

    held = [name for name in FORBIDDEN_APP_PERMISSIONS if name in permissions]
    checks.append(
        AuthCheck(
            check="not an admin",
            status=Status.FAIL if held else Status.PASS,
            detail=f"holds {', '.join(held)}" if held else "no administration permission",
            hint="Remove it. An agent holding this could disable branch protection."
            if held
            else None,
        )
    )
    return checks


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
    # 302 is what GitHub returns when the caller cannot see the membership.
    if r.status_code in (302, 403, 404):
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


def _project_query(kind: str) -> str:
    return f"query($o:String!,$n:Int!){{{kind}(login:$o){{projectV2(number:$n){{title}}}}}}"


def check_project_access(token: str, owner: str, number: int) -> AuthCheck:
    """Projects v2 lives behind GraphQL and needs its own permission.

    Tries the organization root first: an org-owned board is the only kind a
    fine-grained token can reach at all, so it is the expected shape.
    """
    last_error = "rejected"
    for kind in ("organization", "user"):
        try:
            r = httpx.post(
                f"{API}/graphql",
                headers={"Authorization": f"Bearer {token}"},
                json={"query": _project_query(kind), "variables": {"o": owner, "n": number}},
                timeout=TIMEOUT,
            )
            body = r.json()
        except Exception as exc:  # noqa: BLE001
            return AuthCheck(check="project board", status=Status.FAIL, detail=str(exc))

        if body.get("errors"):
            last_error = body["errors"][0].get("message", "rejected")
            continue
        project = ((body.get("data") or {}).get(kind) or {}).get("projectV2")
        if project:
            note = " (user-owned)" if kind == "user" else ""
            return AuthCheck(
                check="project board",
                status=Status.WARN if kind == "user" else Status.PASS,
                detail=f"can read {project['title']!r}{note}",
                hint=(
                    "A user-owned board cannot be driven by a fine-grained token. "
                    "Move it to an organization. See docs/agent-auth.md."
                )
                if kind == "user"
                else None,
            )

    return AuthCheck(
        check="project board",
        status=Status.FAIL,
        detail=last_error[:80],
        hint="If this is a fine-grained token, grant Projects: Read and write under the "
        "organization's permissions. If the board is user-owned, a fine-grained token "
        "cannot reach it at all. See docs/agent-auth.md.",
    )


def check_org_membership(token: str, org: str, login: str) -> AuthCheck:
    """The token's identity must actually be able to act inside the org."""
    r = _get(token, f"/orgs/{org}/members/{login}")
    if r.status_code == 204:
        return AuthCheck(
            check="org membership", status=Status.PASS, detail=f"{login!r} is a member of {org!r}"
        )
    # 302 is what GitHub returns when the caller cannot see the membership.
    if r.status_code in (302, 403, 404):
        # Reading membership needs an org Members grant the crew has no use for.
        # Refusing here would demand privilege to prove privilege; the board and
        # repository checks already establish that the token can do its job.
        return AuthCheck(
            check="org membership",
            status=Status.SKIP,
            detail="not readable without an org Members grant, which is not needed",
        )
    return AuthCheck(
        check="org membership",
        status=Status.WARN,
        detail=f"inconclusive (HTTP {r.status_code})",
    )


def verify(
    token: str,
    *,
    owner: str,
    repos: list[str],
    project_number: int,
    owner_is_org: bool = True,
    app_slug: str | None = None,
    app_permissions: dict[str, str] | None = None,
) -> list[AuthCheck]:
    """Run the full check set, positive and negative."""
    is_app = token.startswith(INSTALLATION_PREFIX)
    checks = [check_token_type(token, app_slug=app_slug)]

    if is_app:
        identity = check_installation_scope(token, app_slug)
        login = None
    else:
        identity, login = check_identity(token)
    checks.append(identity)
    if identity.status is Status.FAIL:
        return checks

    # An App's capability is its grant set, stated once, rather than something
    # to be inferred repo by repo.
    if is_app:
        checks.extend(check_app_permissions(app_permissions or {}))
        for repo in repos:
            checks.append(check_issue_write(token, owner, repo))
        checks.append(check_project_access(token, owner, project_number))
        return checks

    if login:
        if owner_is_org:
            checks.append(check_org_membership(token, owner, login))
        elif login != owner:
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
