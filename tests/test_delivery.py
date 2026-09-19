"""The Developer's output is written straight to a worktree, so the path guard
is a security boundary, not a nicety. It is tested as one."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crew_org.crews.delivery_crew import (
    MAX_FILE_BYTES,
    FileWrite,
    FirstAttempt,
    Implementation,
)


def code(path: str = "src/pkg/mod.py") -> FileWrite:
    return FileWrite(path=path, content="def f():\n    return 1\n")


def a_test(path: str = "tests/test_mod.py") -> FileWrite:
    return FileWrite(path=path, content="def test_f():\n    assert True\n")


# --- the path guard ------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "a/../../b",
        "..",
        "./../x",
        "src/../../out",
        r"..\..\windows",
    ],
)
def test_paths_escaping_the_repository_are_refused(path):
    """Regression: an earlier guard used lstrip('./'), which strips any run of
    '.' and '/' — turning '../../etc/passwd' into 'etc/passwd' and passing."""
    with pytest.raises(ValidationError):
        FileWrite(path=path, content="x")


def test_writing_inside_dot_git_is_refused():
    with pytest.raises(ValidationError, match="refusing to write inside .git"):
        FileWrite(path=".git/config", content="x")


def test_a_leading_dot_slash_is_normalised_not_stripped_character_wise():
    assert FileWrite(path="./src/a.py", content="x").path == "src/a.py"


def test_ordinary_paths_are_allowed():
    assert FileWrite(path="src/pkg/mod.py", content="x").path == "src/pkg/mod.py"


def test_an_empty_path_is_refused():
    with pytest.raises(ValidationError):
        FileWrite(path="   ", content="x")


# --- content -------------------------------------------------------------


def test_empty_content_is_refused():
    """Content is written verbatim, so an empty file silently destroys the original."""
    with pytest.raises(ValidationError, match="content is empty"):
        FileWrite(path="src/a.py", content="   \n")


def test_an_enormous_file_is_refused():
    with pytest.raises(ValidationError, match="Split the work"):
        FileWrite(path="src/a.py", content="x" * (MAX_FILE_BYTES + 1))


# --- Definition of Done --------------------------------------------------


def test_a_first_attempt_without_a_test_is_refused():
    """DoD §7.1 enforced as a schema rule, so it is a SCHEMA failure the repair
    loop handles rather than something a reviewer catches later."""
    with pytest.raises(ValidationError, match="no test"):
        FirstAttempt(summary="s", new_files=[code()])


def test_a_first_attempt_with_a_test_is_accepted():
    impl = FirstAttempt(summary="s", new_files=[code(), a_test()])
    assert len(impl.new_files) == 2


@pytest.mark.parametrize("name", ["tests/test_mod.py", "src/pkg/mod_test.py"])
def test_both_test_naming_conventions_count(name):
    assert FirstAttempt(summary="s", new_files=[code(), a_test(name)]).new_files


def test_a_repair_may_return_the_fix_alone():
    """A repair is told to return only the edits that fix the failure, and the
    test it wrote first time is already in the worktree. Requiring a test in
    every submission asks it to choose which instruction to disobey — story #11
    chose correctly, returned the fix alone, and was rejected for it twice.

    QA is the real guard here: it reads the worktree and will not accept a story
    until it can name the test proving each criterion."""
    impl = Implementation(summary="fix main()", new_files=[code()])
    assert impl.new_files


def test_a_repair_still_has_to_do_something():
    with pytest.raises(ValidationError, match="create a file or edit one"):
        Implementation(summary="nothing to do")


def test_an_empty_implementation_is_refused():
    with pytest.raises(ValidationError, match="create a file or edit one"):
        Implementation(summary="s")


def test_a_test_added_to_an_existing_file_satisfies_the_rule():
    """A story extending a module usually adds cases, not a whole test file."""
    from crew_org.crews.delivery_crew import FileEdit

    impl = Implementation(
        summary="s",
        edits=[
            FileEdit(path="src/m.py", operation="add", target="f", source="def f():\n    pass"),
            FileEdit(
                path="tests/test_m.py",
                operation="add",
                target="test_f",
                source="def test_f():\n    assert True",
            ),
        ],
    )
    assert len(impl.edits) == 2


def test_an_edit_without_source_is_refused_unless_deleting():
    from crew_org.crews.delivery_crew import FileEdit

    with pytest.raises(ValidationError, match="needs source"):
        FileEdit(path="src/m.py", operation="replace", target="f")
    assert FileEdit(path="src/m.py", operation="delete", target="f").target == "f"


def test_an_edit_path_cannot_escape_the_repository():
    from crew_org.crews.delivery_crew import FileEdit

    with pytest.raises(ValidationError):
        FileEdit(path="../../etc/passwd", operation="add", target="f", source="x = 1")


# --- what a repair is told ------------------------------------------------


def _repair_prompt(feedback: str = "1 failed") -> str:
    """The task description the Developer sees on a repair."""
    import crew_org.crews.delivery_crew as dc

    captured: dict[str, str] = {}

    class FakeTask:
        def __init__(self, *, description, **_kw):
            captured["description"] = description

    original_task, original_crew = dc.Task, dc.Crew
    dc.Task = FakeTask
    try:
        dc.implement_story("a story", context="### Files", feedback=feedback)
    except Exception:  # noqa: BLE001, S110 — only the prompt is under test
        pass
    finally:
        dc.Task, dc.Crew = original_task, original_crew
    return captured.get("description", "")


def test_a_repair_is_told_its_previous_attempt_is_already_written():
    """Story #10 tried to `add` a test its own earlier attempt had added, then
    returned nothing at all. The worktree accumulates across attempts, so a
    repair that does not know this re-sends work that already landed."""
    prompt = _repair_prompt()
    assert "ALREADY BEEN WRITTEN" in prompt
    assert "replace" in prompt


def test_a_repair_is_not_asked_for_whole_files():
    """`Return the complete corrected files` survived from the whole-file
    schema (46fb8c6) through the move to editing by name (f8bb521), and
    contradicted the standing instructions on every repair."""
    prompt = _repair_prompt()
    assert "complete corrected files" not in prompt
    assert "Return ONLY the edits" in prompt


def test_a_first_attempt_carries_no_repair_block():
    """The repair text must not enter the cacheable prefix of a fresh attempt."""
    assert "ALREADY BEEN WRITTEN" not in _repair_prompt(feedback="")
