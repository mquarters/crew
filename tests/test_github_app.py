"""The App identity is what makes the audit trail honest — the crew's work must
be attributed to the crew, not to the Sponsor — so its failure messages have to
say what is actually wrong."""

from __future__ import annotations

import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from crew_org.github_app import AppAuthError, AppCredentials, AppTokenProvider


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def creds(private_key) -> AppCredentials:
    return AppCredentials(app_id="12345", private_key=private_key, installation_id="99")


def provider_for(creds, handler) -> AppTokenProvider:
    return AppTokenProvider(creds, client=httpx.Client(transport=httpx.MockTransport(handler)))


# --- configuration -------------------------------------------------------


def test_no_app_configured_returns_none_so_the_pat_is_used():
    assert AppCredentials.from_env({}) is None
    assert AppCredentials.from_env({"GITHUB_APP_ID": "1"}) is None


def test_a_missing_private_key_says_where_it_looked(tmp_path):
    env = {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": str(tmp_path / "absent.pem")}
    with pytest.raises(AppAuthError, match="does not exist"):
        AppCredentials.from_env(env)


def test_credentials_load_from_disk(tmp_path, private_key):
    path = tmp_path / "key.pem"
    path.write_text(private_key)
    creds = AppCredentials.from_env({"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": str(path)})
    assert creds is not None and creds.app_id == "42"


def test_a_prefix_selects_a_different_app(tmp_path, private_key):
    """The crew reviews as a second app because GitHub refuses an approval from
    the identity that opened the pull request."""
    delivery = tmp_path / "delivery.pem"
    delivery.write_text(private_key)
    reviewer = tmp_path / "reviewer.pem"
    reviewer.write_text(private_key)
    env = {
        "GITHUB_APP_ID": "42",
        "GITHUB_APP_PRIVATE_KEY": str(delivery),
        "GITHUB_REVIEW_APP_ID": "99",
        "GITHUB_REVIEW_APP_PRIVATE_KEY": str(reviewer),
        "GITHUB_REVIEW_APP_INSTALLATION_ID": "7",
    }
    assert AppCredentials.from_env(env).app_id == "42"
    review = AppCredentials.from_env(env, prefix="GITHUB_REVIEW_APP_")
    assert review is not None
    assert review.app_id == "99"
    assert review.installation_id == "7"


def test_an_unconfigured_prefix_is_none_rather_than_the_default(tmp_path, private_key):
    """Falling back is resolve_credentials' decision to make, not this one's."""
    path = tmp_path / "key.pem"
    path.write_text(private_key)
    env = {"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": str(path)}
    assert AppCredentials.from_env(env, prefix="GITHUB_REVIEW_APP_") is None


# --- signing -------------------------------------------------------------


def test_a_bad_private_key_is_reported_as_such():
    bad = AppCredentials(app_id="1", private_key="not a pem")
    with pytest.raises(AppAuthError, match="could not sign"):
        provider_for(bad, lambda r: httpx.Response(200))._app_jwt()


def test_the_jwt_stays_inside_githubs_ten_minute_limit(creds):
    import jwt as pyjwt

    token = provider_for(creds, lambda r: httpx.Response(200))._app_jwt()
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] <= 10 * 60


# --- tokens --------------------------------------------------------------


def test_an_installation_token_is_minted(creds):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/99/access_tokens"
        return httpx.Response(201, json={"token": "ghs_installation"})

    assert provider_for(creds, handler).token() == "ghs_installation"


def test_a_valid_token_is_reused_rather_than_reminted(creds):
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(201, json={"token": "ghs_x"})

    p = provider_for(creds, handler)
    p.token()
    p.token()
    assert calls["n"] == 1


def test_a_token_near_expiry_is_reminted(creds):
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(201, json={"token": "ghs_x"})

    p = provider_for(creds, handler)
    p.token()
    p._expires_at = time.time() + 60  # inside the refresh margin
    p.token()
    assert calls["n"] == 2


def test_a_refused_mint_reports_githubs_reason(creds):
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Bad credentials"})

    with pytest.raises(AppAuthError, match="Bad credentials"):
        provider_for(creds, handler).token()


# --- installation discovery ----------------------------------------------


def test_installation_is_discovered_when_not_configured(private_key):
    creds = AppCredentials(app_id="1", private_key=private_key)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 777, "account": {"login": "mqucifer"}}])
        return httpx.Response(201, json={"token": "ghs_x"})

    assert provider_for(creds, handler).installation_id() == "777"


def test_an_uninstalled_app_says_to_install_it(private_key):
    creds = AppCredentials(app_id="1", private_key=private_key)
    with pytest.raises(AppAuthError, match="not installed anywhere"):
        provider_for(creds, lambda r: httpx.Response(200, json=[])).installation_id()


def test_several_installations_ask_which_one(private_key):
    creds = AppCredentials(app_id="1", private_key=private_key)
    installs = [
        {"id": 1, "account": {"login": "mqucifer"}},
        {"id": 2, "account": {"login": "other"}},
    ]
    with pytest.raises(AppAuthError, match="GITHUB_APP_INSTALLATION_ID"):
        provider_for(creds, lambda r: httpx.Response(200, json=installs)).installation_id()


def test_the_bot_identity_is_reported(creds):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app":
            return httpx.Response(200, json={"slug": "crew"})
        return httpx.Response(201, json={"token": "x"})

    assert provider_for(creds, handler).identity() == "crew[bot]"
