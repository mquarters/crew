"""The negative checks are the reason this module exists, so they carry the
tests: an over-privileged token looks exactly like a correct one until an agent
uses it."""

from __future__ import annotations

import httpx
import pytest

from crew_org import auth
from crew_org.auth import Status, check_not_admin, check_repo_access, check_token_type, load_token

OWNER, REPO = "mquarters", "crew"
FINE = "github_pat_" + "x" * 22
CLASSIC = "ghp_" + "y" * 36


def response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status, json=payload or {}, request=httpx.Request("GET", "https://api.github.com/x")
    )


# --- token type ----------------------------------------------------------


def test_fine_grained_token_passes():
    assert check_token_type(FINE).status is Status.PASS


def test_classic_token_warns_about_blast_radius():
    result = check_token_type(CLASSIC)
    assert result.status is Status.WARN
    assert "every repository" in result.detail


# --- the negative check --------------------------------------------------


@pytest.mark.parametrize("status", [403, 404])
def test_a_token_refused_administration_passes(monkeypatch, status):
    monkeypatch.setattr(auth, "_get", lambda *a, **k: response(status))
    assert check_not_admin(FINE, OWNER, REPO).status is Status.PASS


def test_a_token_that_can_administer_fails(monkeypatch):
    """This is the check the whole module exists for."""
    monkeypatch.setattr(auth, "_get", lambda *a, **k: response(200, {"enabled": True}))
    result = check_not_admin(FINE, OWNER, REPO)
    assert result.status is Status.FAIL
    assert "branch protection" in (result.hint or "")


# --- repo access ---------------------------------------------------------


def test_repo_not_in_the_token_allow_list_fails(monkeypatch):
    monkeypatch.setattr(auth, "_get", lambda *a, **k: response(404))
    assert check_repo_access(FINE, OWNER, REPO).status is Status.FAIL


def test_read_only_token_fails_because_agents_must_push(monkeypatch):
    monkeypatch.setattr(
        auth, "_get", lambda *a, **k: response(200, {"permissions": {"push": False}})
    )
    result = check_repo_access(FINE, OWNER, REPO)
    assert result.status is Status.FAIL
    assert "Contents" in (result.hint or "")


def test_push_capable_token_passes(monkeypatch):
    monkeypatch.setattr(
        auth, "_get", lambda *a, **k: response(200, {"permissions": {"push": True}})
    )
    assert check_repo_access(FINE, OWNER, REPO).status is Status.PASS


# --- loading -------------------------------------------------------------


def test_token_is_read_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"# comment\nGITHUB_OWNER=mquarters\nGITHUB_TOKEN={FINE}\n")
    assert load_token(env) == FINE


def test_environment_wins_over_the_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    env = tmp_path / ".env"
    env.write_text(f"GITHUB_TOKEN={FINE}\n")
    assert load_token(env) == "from-env"


def test_missing_token_is_none_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert load_token(tmp_path / "nonexistent") is None


def test_commented_out_token_is_not_used(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"#GITHUB_TOKEN={FINE}\n")
    assert load_token(env) is None


# --- organization membership --------------------------------------------


def test_org_member_passes(monkeypatch):
    monkeypatch.setattr(auth, "_get", lambda *a, **k: response(204))
    assert auth.check_org_membership(FINE, "mqucifer", "someone").status is Status.PASS


def test_unreadable_membership_is_skipped_not_failed(monkeypatch):
    """Reading membership needs a grant the crew has no use for. Demanding it
    would require privilege in order to prove privilege."""
    monkeypatch.setattr(auth, "_get", lambda *a, **k: response(403))
    assert auth.check_org_membership(FINE, "mqucifer", "someone").status is Status.SKIP


def test_org_owner_does_not_trigger_the_owner_match_warning(monkeypatch):
    """Against an organization the login never matches, so that warning is noise."""
    monkeypatch.setattr(auth, "_get", lambda *a, **k: response(204))
    monkeypatch.setattr(
        auth,
        "check_identity",
        lambda t: (auth.AuthCheck(check="identity", status=Status.PASS, detail="x"), "mquarters"),
    )
    monkeypatch.setattr(
        auth,
        "check_project_access",
        lambda *a, **k: auth.AuthCheck(check="project board", status=Status.PASS, detail="x"),
    )
    checks = auth.verify(FINE, owner="mqucifer", repos=[], project_number=1, owner_is_org=True)
    assert not any(c.check == "owner match" for c in checks)
    assert any(c.check == "org membership" for c in checks)
