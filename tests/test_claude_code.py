"""Escalation runs on the Sponsor's subscription, so a usage limit is an
ordinary outcome to park and retry — never an error, and never a bill."""

from __future__ import annotations

import json
import subprocess

import pytest

from crew_org.tools import claude_code
from crew_org.tools.claude_code import Outcome, escalate


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def with_result(monkeypatch, **kwargs):
    monkeypatch.setattr(claude_code, "available", lambda command="claude": True)
    monkeypatch.setattr(claude_code.subprocess, "run", lambda *a, **k: FakeCompleted(**kwargs))


def success_payload(**extra) -> str:
    return json.dumps({"result": "done", "is_error": False, **extra})


# --- the happy path ------------------------------------------------------


def test_a_completed_run_reports_its_cost_and_turns(monkeypatch, tmp_path):
    with_result(monkeypatch, stdout=success_payload(total_cost_usd=0.42, num_turns=7))
    result = escalate(tmp_path, "fix it")
    assert result.ok
    assert result.cost_usd == 0.42 and result.turns == 7


def test_unparseable_output_from_a_successful_run_still_counts(monkeypatch, tmp_path):
    """The worktree is the real result; the transcript is commentary."""
    with_result(monkeypatch, stdout="not json at all")
    assert escalate(tmp_path, "fix it").outcome is Outcome.COMPLETED


# --- rate limits ---------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["You have hit your usage limit", "rate limit exceeded", "Resets at 4pm", "Too many requests"],
)
def test_a_usage_limit_is_parked_not_failed(monkeypatch, tmp_path, text):
    with_result(monkeypatch, stderr=text, returncode=1)
    result = escalate(tmp_path, "fix it")
    assert result.outcome is Outcome.RATE_LIMITED
    assert result.should_park
    assert not result.ok


def test_a_rate_limit_inside_a_json_error_is_recognised(monkeypatch, tmp_path):
    with_result(
        monkeypatch,
        stdout=json.dumps({"is_error": True, "result": "Claude usage limit reached"}),
    )
    assert escalate(tmp_path, "fix it").outcome is Outcome.RATE_LIMITED


def test_an_ordinary_error_is_a_failure_not_a_park(monkeypatch, tmp_path):
    with_result(monkeypatch, stdout=json.dumps({"is_error": True, "result": "syntax error"}))
    result = escalate(tmp_path, "fix it")
    assert result.outcome is Outcome.FAILED
    assert not result.should_park


# --- unavailability ------------------------------------------------------


def test_a_missing_cli_is_reported_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_code, "available", lambda command="claude": False)
    result = escalate(tmp_path, "fix it")
    assert result.outcome is Outcome.UNAVAILABLE
    assert "not on PATH" in result.detail


def test_a_hanging_escalation_is_killed(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_code, "available", lambda command="claude": True)

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(claude_code.subprocess, "run", hang)
    assert escalate(tmp_path, "fix it", timeout=1).outcome is Outcome.TIMED_OUT


# --- what it may touch ---------------------------------------------------


def test_escalation_cannot_push():
    """Landing work stays with git_ops, which refuses to write to main."""
    assert "git" not in claude_code.ALLOWED_TOOLS.lower()


def test_credentials_are_stripped_from_the_subprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setattr(claude_code, "available", lambda command="claude": True)
    seen: dict = {}

    def capture(*a, **k):
        seen.update(k.get("env") or {})
        return FakeCompleted(stdout=success_payload())

    monkeypatch.setattr(claude_code.subprocess, "run", capture)
    escalate(tmp_path, "fix it")
    assert "GITHUB_TOKEN" not in seen


def test_no_anthropic_api_key_is_ever_passed(monkeypatch, tmp_path):
    """The cost guarantee is structural: escalation uses subscription OAuth."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
    monkeypatch.setattr(claude_code, "available", lambda command="claude": True)
    seen: dict = {}

    def capture(*a, **k):
        seen.update(k.get("env") or {})
        return FakeCompleted(stdout=success_payload())

    monkeypatch.setattr(claude_code.subprocess, "run", capture)
    escalate(tmp_path, "fix it")
    assert "ANTHROPIC_API_KEY" not in seen
