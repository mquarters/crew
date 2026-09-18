"""GitHub App identity for the crew.

A personal access token always acts as the human who created it, so every issue,
comment and commit the crew made was attributed to the Sponsor. That defeats the
separation the whole design rests on: the audit trail should say what the *crew*
did, and the Sponsor should be able to approve the crew's pull requests — which
GitHub forbids when author and reviewer are the same account.

A GitHub App has its own `<slug>[bot]` identity, costs no seat, and mints
short-lived installation tokens rather than holding a long-lived credential.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt

API = "https://api.github.com"
# GitHub rejects app JWTs with more than 10 minutes of life. Nine leaves room
# for clock skew between here and GitHub.
JWT_LIFETIME = 9 * 60
# Installation tokens last an hour; refresh early so a long tick never fails
# halfway through holding an expired one.
REFRESH_MARGIN = 5 * 60


class AppAuthError(RuntimeError):
    pass


@dataclass
class AppCredentials:
    app_id: str
    private_key: str
    installation_id: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppCredentials | None:
        """Read app credentials, or return None if the crew is still on a PAT."""
        env = env if env is not None else dict(os.environ)
        app_id = env.get("GITHUB_APP_ID")
        key_path = env.get("GITHUB_APP_PRIVATE_KEY")
        if not app_id or not key_path:
            return None

        path = Path(key_path).expanduser()
        if not path.exists():
            raise AppAuthError(
                f"GITHUB_APP_PRIVATE_KEY points at {path}, which does not exist. "
                "Download the app's private key and place it there (it is gitignored)."
            )
        return cls(
            app_id=app_id,
            private_key=path.read_text(encoding="utf-8"),
            installation_id=env.get("GITHUB_APP_INSTALLATION_ID") or None,
        )


class AppTokenProvider:
    """Mints and caches installation access tokens."""

    def __init__(self, creds: AppCredentials, *, client: httpx.Client | None = None) -> None:
        self.creds = creds
        self._client = client or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._expires_at: float = 0.0
        # The mint response states exactly what this installation granted —
        # a more honest source than probing each repository, because an
        # installation token does not report per-repo push rights the way a
        # personal access token does.
        self.permissions: dict[str, str] = {}
        self.repositories: str = "unknown"

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + JWT_LIFETIME, "iss": self.creds.app_id}
        try:
            return jwt.encode(payload, self.creds.private_key, algorithm="RS256")
        except Exception as exc:  # noqa: BLE001
            raise AppAuthError(
                f"could not sign the app JWT: {exc}. The private key must be the PEM "
                "GitHub generated for this app."
            ) from exc

    def _headers(self, bearer: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def installation_id(self) -> str:
        """The app's installation, discovered if not configured."""
        if self.creds.installation_id:
            return self.creds.installation_id

        r = self._client.get(f"{API}/app/installations", headers=self._headers(self._app_jwt()))
        if r.status_code != 200:
            raise AppAuthError(
                f"could not list installations ({r.status_code}): "
                f"{r.json().get('message', r.text[:100])}. Check GITHUB_APP_ID."
            )
        installations = r.json()
        if not installations:
            raise AppAuthError(
                "the app exists but is not installed anywhere. Install it on the "
                "organization and select the crew's repositories."
            )
        if len(installations) > 1:
            accounts = ", ".join(i["account"]["login"] for i in installations)
            raise AppAuthError(
                f"the app is installed on several accounts ({accounts}). "
                "Set GITHUB_APP_INSTALLATION_ID to choose one."
            )
        self.creds.installation_id = str(installations[0]["id"])
        return self.creds.installation_id

    def token(self) -> str:
        """A valid installation token, minted or reused."""
        if self._token and time.time() < self._expires_at - REFRESH_MARGIN:
            return self._token

        r = self._client.post(
            f"{API}/app/installations/{self.installation_id()}/access_tokens",
            headers=self._headers(self._app_jwt()),
        )
        if r.status_code != 201:
            raise AppAuthError(
                f"could not mint an installation token ({r.status_code}): "
                f"{r.json().get('message', r.text[:100])}"
            )
        body = r.json()
        self.permissions = body.get("permissions") or {}
        self.repositories = body.get("repository_selection", "unknown")
        self._token = body["token"]
        # expires_at is ISO8601; an hour from now is the documented lifetime.
        self._expires_at = time.time() + 3600
        return self._token

    def identity(self) -> str:
        """The bot login this app acts as, e.g. 'crew[bot]'."""
        r = self._client.get(f"{API}/app", headers=self._headers(self._app_jwt()))
        if r.status_code != 200:
            raise AppAuthError(f"could not read the app ({r.status_code})")
        return f"{r.json()['slug']}[bot]"
