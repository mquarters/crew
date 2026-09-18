"""The Developer's output is written straight to a worktree, so the path guard
is a security boundary, not a nicety. It is tested as one."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crew_org.crews.delivery_crew import MAX_FILE_BYTES, FileWrite, Implementation


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


def test_an_implementation_without_a_test_is_refused():
    """DoD §7.1 enforced as a schema rule, so it is a SCHEMA failure the repair
    loop handles rather than something a reviewer catches later."""
    with pytest.raises(ValidationError, match="no test file"):
        Implementation(summary="s", files=[code()])


def test_an_implementation_with_a_test_is_accepted():
    impl = Implementation(summary="s", files=[code(), a_test()])
    assert len(impl.files) == 2


@pytest.mark.parametrize("name", ["tests/test_mod.py", "src/pkg/mod_test.py"])
def test_both_test_naming_conventions_count(name):
    assert Implementation(summary="s", files=[code(), a_test(name)]).files


def test_an_empty_implementation_is_refused():
    with pytest.raises(ValidationError, match="at least one file"):
        Implementation(summary="s", files=[])


def test_the_same_path_twice_is_refused():
    """Each file is written in full, so a second write would silently win."""
    with pytest.raises(ValidationError, match="written twice"):
        Implementation(summary="s", files=[code(), a_test(), code()])
