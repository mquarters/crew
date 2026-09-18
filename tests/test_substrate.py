"""The Phase 0 probes are what decides whether the architecture is viable, so
their verdicts are tested against simulated servers — especially the failure
verdicts, which are the ones that matter."""

from __future__ import annotations

import json

import httpx
import pytest

from crew_org import substrate as sub
from crew_org.substrate import Status

BASE = "http://spark:30000/v1"
MODEL = "Qwen3.8-27B"


REQUEST = httpx.Request("POST", f"{BASE}/chat/completions")


def chat_response(message: dict) -> httpx.Response:
    # A Response needs its request attached or raise_for_status() errors.
    return httpx.Response(200, json={"choices": [{"message": message}]}, request=REQUEST)


@pytest.fixture
def server(monkeypatch):
    """Install a fake OpenAI-compatible server. Handlers keyed by endpoint."""
    state: dict[str, object] = {}

    def fake_get(url, **_kw):
        if state.get("unreachable"):
            raise httpx.ConnectError("refused")
        return httpx.Response(
            200,
            json={"data": [{"id": MODEL}]},
            request=httpx.Request("GET", f"{BASE}/models"),
        )

    monkeypatch.setattr(sub.httpx, "get", fake_get)
    return state


def install_post(monkeypatch, handler):
    def fake_post(url, *, json=None, **_kw):
        return handler(json)

    monkeypatch.setattr(sub.httpx, "post", fake_post)


# --- reachability --------------------------------------------------------


def test_unreachable_endpoint_fails_and_skips_the_rest(monkeypatch):
    monkeypatch.setattr(
        sub.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    )
    results = sub.run_all(BASE)
    assert results[0].status is Status.FAIL
    assert [r.status for r in results[1:]] == [Status.SKIP] * 3


def test_served_model_name_is_discovered(server, monkeypatch):
    install_post(monkeypatch, lambda _b: chat_response({"content": "ready"}))
    result, served = sub.probe_models(BASE)
    assert result.status is Status.PASS
    assert served == MODEL


# --- tool calling (0c) ---------------------------------------------------


def test_prose_instead_of_tool_calls_is_a_failure_with_the_parser_hint(server, monkeypatch):
    install_post(
        monkeypatch, lambda _b: chat_response({"content": "Sure, I'll move card 42 for you."})
    )
    r = sub.probe_tool_calling(BASE, MODEL)
    assert r.status is Status.FAIL
    assert "tool-call-parser" in (r.hint or "")


def test_unparseable_tool_arguments_are_a_failure(server, monkeypatch):
    install_post(
        monkeypatch,
        lambda _b: chat_response(
            {"tool_calls": [{"function": {"name": "set_card_status", "arguments": "{not json"}}]}
        ),
    )
    assert sub.probe_tool_calling(BASE, MODEL).status is Status.FAIL


def test_incomplete_tool_arguments_warn_rather_than_fail(server, monkeypatch):
    install_post(
        monkeypatch,
        lambda _b: chat_response(
            {
                "tool_calls": [
                    {"function": {"name": "set_card_status", "arguments": json.dumps({"card": 42})}}
                ]
            }
        ),
    )
    assert sub.probe_tool_calling(BASE, MODEL).status is Status.WARN


def test_well_formed_tool_call_passes(server, monkeypatch):
    install_post(
        monkeypatch,
        lambda _b: chat_response(
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "set_card_status",
                            "arguments": json.dumps({"card": 42, "status": "In Review"}),
                        }
                    }
                ]
            }
        ),
    )
    assert sub.probe_tool_calling(BASE, MODEL).status is Status.PASS


# --- constrained JSON (0d) ----------------------------------------------

VALID_STORY = {"title": "Report cycle time", "points": 3, "acceptance_criteria": ["given…"]}


def test_consistently_schema_valid_output_passes(server, monkeypatch):
    install_post(monkeypatch, lambda _b: chat_response({"content": json.dumps(VALID_STORY)}))
    r = sub.probe_structured_output(BASE, MODEL, trials=5)
    assert r.status is Status.PASS
    assert "5/5" in r.detail


def test_never_valid_output_fails_with_the_grammar_backend_hint(server, monkeypatch):
    install_post(monkeypatch, lambda _b: chat_response({"content": "Here is a story: ..."}))
    r = sub.probe_structured_output(BASE, MODEL, trials=5)
    assert r.status is Status.FAIL
    assert "grammar" in (r.hint or "").lower()


def test_intermittently_valid_output_warns_because_one_pass_proves_nothing(server, monkeypatch):
    calls = {"n": 0}

    def handler(_body):
        calls["n"] += 1
        content = json.dumps(VALID_STORY) if calls["n"] % 2 else "not json"
        return chat_response({"content": content})

    install_post(monkeypatch, handler)
    r = sub.probe_structured_output(BASE, MODEL, trials=5)
    assert r.status is Status.WARN


def test_missing_required_keys_is_not_counted_as_valid(server, monkeypatch):
    install_post(monkeypatch, lambda _b: chat_response({"content": json.dumps({"title": "x"})}))
    assert sub.probe_structured_output(BASE, MODEL, trials=3).status is Status.FAIL
