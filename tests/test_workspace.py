"""Applying and checking an implementation. The guards matter because this code
writes files the model chose and then executes them."""

from __future__ import annotations

import pytest

from crew_org.crews.delivery_crew import FileWrite
from crew_org.tools import workspace
from crew_org.tools.sandbox import Mode, Sandbox
from crew_org.tools.workspace import CheckResult, CommandResult, apply, check

# These exercise the runner itself — argument handling, timeouts, output caps —
# so they run on the host deliberately. Spinning up a container per assertion
# would make the suite slow and dependent on a running daemon while testing
# nothing about the container. The sandbox has its own tests.
HOST = Sandbox(mode=Mode.OFF)


def run(worktree, command, **kwargs):
    return workspace.run(worktree, command, sandbox=HOST, **kwargs)


def test_files_are_written_into_the_worktree(tmp_path):
    written = apply(tmp_path, [FileWrite(path="src/pkg/mod.py", content="x = 1\n")])
    assert written == ["src/pkg/mod.py"]
    assert (tmp_path / "src/pkg/mod.py").read_text() == "x = 1\n"


def test_nested_directories_are_created(tmp_path):
    apply(tmp_path, [FileWrite(path="a/b/c/d.py", content="y\n")])
    assert (tmp_path / "a/b/c/d.py").exists()


def test_an_existing_file_is_replaced_in_full(tmp_path):
    (tmp_path / "f.py").write_text("old and much longer\n")
    apply(tmp_path, [FileWrite(path="f.py", content="new\n")])
    assert (tmp_path / "f.py").read_text() == "new\n"


def test_a_symlinked_escape_is_refused(tmp_path):
    """The schema blocks '..' in a path, but a symlink can still point outside.
    Containment is re-checked after resolution because the cost of being wrong
    is writing outside the repository."""
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the worktree"):
        apply(tmp_path, [FileWrite(path="link/escaped.py", content="x\n")])


# --- running -------------------------------------------------------------


def test_a_successful_command_is_ok(tmp_path):
    result = run(tmp_path, ["python3", "-c", "print('hello')"])
    assert result.ok and "hello" in result.output


def test_a_failing_command_carries_its_output(tmp_path):
    result = run(tmp_path, ["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])
    assert not result.ok
    assert result.code == 2
    assert "boom" in result.output


def test_a_hanging_command_is_killed(tmp_path):
    """A runaway test must not hang the tick."""
    result = run(tmp_path, ["python3", "-c", "import time; time.sleep(30)"], timeout=1)
    assert result.timed_out and not result.ok


def test_a_missing_command_is_reported_not_raised(tmp_path):
    assert run(tmp_path, ["definitely-not-a-command"]).code == 127


def test_credentials_are_stripped_even_on_the_host_path(tmp_path, monkeypatch):
    """Host execution is an explicit opt-in, not a safe one — it must still
    refuse to hand a repository token to code the model wrote."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("HARMLESS", "visible")
    result = run(
        tmp_path,
        [
            "python3",
            "-c",
            "import os; print(os.environ.get('GITHUB_TOKEN'), os.environ.get('HARMLESS'))",
        ],
    )
    assert "ghs_secret" not in result.output
    assert "None visible" in result.output


def test_command_output_is_kept_whole(tmp_path):
    """A 3,000-character head and tail was enough to repair from only if the
    defect sat at one end. Story #9's pytest run had thirteen failures and the
    model repaired three times from a view with the middle cut out."""
    result = run(
        tmp_path, ["python3", "-c", "print('A'*50_000); print('MIDDLE'); print('B'*50_000)"]
    )

    assert "MIDDLE" in result.output
    assert len(result.output) > 100_000


def test_a_runaway_report_keeps_the_end(tmp_path):
    """The guard is against a command that will not stop, not against size. A
    test run puts its summary and its last failure at the end."""
    from crew_org.tools.workspace import CheckResult, CommandResult

    huge = "x" * (workspace.MAX_FAILURE_REPORT_CHARS + 10_000)
    check = CheckResult(
        results=[CommandResult(command="pytest -q", code=1, output=huge + "\nFAILED test_last")]
    )

    report = check.failure_report
    assert report.endswith("FAILED test_last")
    assert "dropped from the start" in report
    assert len(report) < workspace.MAX_FAILURE_REPORT_CHARS * 2


# --- verdicts ------------------------------------------------------------


def ok(cmd: str) -> CommandResult:
    return CommandResult(command=cmd, code=0, output="")


def bad(cmd: str, output: str = "failed") -> CommandResult:
    return CommandResult(command=cmd, code=1, output=output)


def test_all_green_is_ok():
    assert CheckResult(results=[ok("ruff"), ok("pytest")]).ok


def test_any_failure_fails_the_check():
    assert not CheckResult(results=[ok("ruff"), bad("pytest")]).ok


def test_the_failure_report_carries_only_what_broke():
    report = CheckResult(results=[ok("ruff"), bad("pytest", "2 failed")]).failure_report
    assert "pytest" in report and "2 failed" in report
    assert "ruff" not in report


def test_a_timeout_is_named_as_such_in_the_report():
    timed = CommandResult(command="pytest", code=-1, output="no output", timed_out=True)
    assert "timed out" in CheckResult(results=[timed]).failure_report


# --- the autofix pass ----------------------------------------------------


@pytest.fixture
def recorded(monkeypatch):
    """Record what check() runs, and let each command's exit code be chosen."""
    calls: list[list[str]] = []
    codes: dict[str, int] = {}

    def fake_run(worktree, command, **_kwargs):
        calls.append(command)
        key = " ".join(command)
        return CommandResult(
            command=key, code=codes.get(key, 0), output=codes.get(key, 0) and "x" or ""
        )

    monkeypatch.setattr(workspace, "run", fake_run)
    return calls, codes


def test_the_fixable_is_fixed_before_anything_is_judged(tmp_path, recorded):
    calls, _ = recorded
    check(tmp_path, sandbox=HOST)
    joined = [" ".join(c) for c in calls]
    fix = joined.index("uv run ruff check --fix-only .")
    lint = joined.index("uv run ruff check .")
    assert fix < lint, "autofix must run before the lint that judges"
    assert joined.index("uv run ruff format .") < lint


def test_a_failing_autofix_is_not_a_failing_check(tmp_path, recorded):
    """`ruff check --fix-only` exits non-zero when something is left unfixed.
    That is the lint's verdict to deliver, not the autofix's — otherwise every
    unfixable finding would be reported twice and counted once too often."""
    _, codes = recorded
    codes["uv run ruff check --fix-only ."] = 1
    assert check(tmp_path, sandbox=HOST).ok


def test_the_verdict_is_still_the_lint_and_the_tests(tmp_path, recorded):
    _, codes = recorded
    codes["uv run ruff check ."] = 1
    assert not check(tmp_path, sandbox=HOST).ok


def test_nothing_runs_if_dependencies_do_not_resolve(tmp_path, recorded):
    """Autofixing with no toolchain installed would fail confusingly."""
    calls, codes = recorded
    codes["uv sync --extra dev --quiet"] = 1
    check(tmp_path, sandbox=HOST)
    assert len(calls) == 1


def test_a_failed_sync_stops_before_linting(tmp_path, monkeypatch):
    """Running tests against unresolved dependencies produces noise, not signal."""
    calls: list[list[str]] = []

    def fake_run(worktree, command, *, sandbox=None, network=False, timeout=0):
        calls.append(command)
        return CommandResult(command=" ".join(command), code=1, output="lock conflict")

    monkeypatch.setattr(workspace, "run", fake_run)
    result = check(tmp_path, sandbox=HOST)
    assert len(calls) == 1
    assert not result.ok


def test_the_sandbox_is_required_unless_explicitly_turned_off(tmp_path, monkeypatch):
    """The default path refuses to run when no engine is available, rather than
    quietly executing generated code on the host."""
    monkeypatch.setattr("crew_org.tools.sandbox.shutil.which", lambda _: None)
    result = workspace.run(tmp_path, ["echo", "hi"])
    assert not result.ok
    assert result.code == 126
    assert "not on PATH" in result.output


def test_dependency_resolution_is_the_only_networked_step(tmp_path, monkeypatch):
    """Generated code must not reach anything while it executes."""
    seen: list[tuple[list[str], bool]] = []

    def fake_run(worktree, command, *, sandbox=None, network=False, timeout=0):
        seen.append((command, network))
        return CommandResult(command=" ".join(command), code=0, output="")

    monkeypatch.setattr(workspace, "run", fake_run)
    check(tmp_path, sandbox=HOST)
    assert seen[0][1] is True and "sync" in seen[0][0]
    assert all(networked is False for _cmd, networked in seen[1:])
