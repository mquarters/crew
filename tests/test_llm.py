"""The proxy is the crew's single point of dependency, so `health` is the first
thing that speaks when anything is wrong with it. Its verdicts are tested
because a misdiagnosis here is expensive: it sends you to fix the wrong thing.
"""

from __future__ import annotations

import httpx
import pytest

from crew_org import llm

URL = "http://localhost:4000/v1"
REQUEST = httpx.Request("GET", f"{URL}/models")


@pytest.fixture
def proxy(monkeypatch):
    """Install a fake proxy. Set `state` to choose how it answers."""
    state: dict[str, object] = {"key": None}

    def fake_get(url, **kwargs):
        if state.get("unreachable"):
            raise httpx.ConnectError("refused")
        required = state.get("key")
        sent = kwargs.get("headers", {}).get("Authorization", "")
        if required and sent != f"Bearer {required}":
            return httpx.Response(401, json={"error": "no api key passed in"}, request=REQUEST)
        aliases = state.get("aliases", ["crew-local", "crew-code"])
        return httpx.Response(200, json={"data": [{"id": a} for a in aliases]}, request=REQUEST)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("CREW_LLM_BASE_URL", URL)
    monkeypatch.setenv("CREW_LLM_API_KEY", "sk-not-used")
    return state


def test_an_unreachable_proxy_says_how_to_start_it(proxy):
    proxy["unreachable"] = True
    ok, message = llm.health()
    assert not ok
    assert "not answering" in message
    assert "docker compose" in message
    # Without --env-file the compose guards refuse to start, so a start command
    # that omits it is not a start command.
    assert "--env-file" in message


def test_a_proxy_that_rejects_the_key_is_not_reported_as_down(proxy, monkeypatch):
    """The expensive misdiagnosis: a proxy started with LITELLM_MASTER_KEY
    answers 401, and calling that "not answering" sends you to restart a
    container that is already healthy."""
    proxy["key"] = "sk-the-real-one"
    ok, message = llm.health()
    assert not ok
    assert "not answering" not in message
    assert "rejected the credential" in message
    assert "CREW_LLM_API_KEY" in message


def test_the_configured_key_is_sent(proxy, monkeypatch):
    proxy["key"] = "sk-the-real-one"
    monkeypatch.setenv("CREW_LLM_API_KEY", "sk-the-real-one")
    ok, message = llm.health()
    assert ok, message


def test_a_proxy_missing_the_workhorse_alias_is_not_healthy(proxy):
    proxy["aliases"] = ["something-else"]
    ok, message = llm.health()
    assert not ok
    assert "crew-local" in message


def test_settings_fall_back_to_the_env_file(monkeypatch, tmp_path):
    """`load_env` does not export, so os.environ alone misses `.env` — which is
    exactly where the credential lives."""
    monkeypatch.delenv("CREW_LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CREW_LLM_API_KEY=sk-from-env\n")
    assert llm.api_key() == "sk-from-env"


def test_an_absent_setting_falls_back_to_the_placeholder(monkeypatch, tmp_path):
    monkeypatch.delenv("CREW_LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert llm.api_key() == llm.PLACEHOLDER_KEY
