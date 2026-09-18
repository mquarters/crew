"""Detecting an implementation that destroys existing work.

The Developer returns whole files, so extending an existing module means
rewriting it — and a model asked to add one metric will happily redesign the
module it is adding to. Observed on story #7, which rewrote story #6's merged
code, renamed its public functions, and implemented three future stories.

This is checked mechanically because a prompt asking the model not to do it is a
request, not a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

from crew_org.tools.regression import (
    describe,
    find_regressions,
    public_names,
    removed_public_names,
)


@dataclass
class Written:
    path: str
    content: str


BEFORE = '''
"""Story 6's module, merged and tested."""
from datetime import date

VERSION = 1


class Card:
    pass


def _parse_date(value):
    return None


def calculate_cycle_time_and_lead_time(card):
    return None, None


def format_performance_table(cards):
    return ""
'''


# --- what counts as public -----------------------------------------------


def test_public_names_are_the_ones_a_caller_could_import():
    assert public_names(BEFORE) == {
        "VERSION",
        "Card",
        "calculate_cycle_time_and_lead_time",
        "format_performance_table",
    }


def test_private_names_are_the_authors_business():
    assert "_parse_date" not in public_names(BEFORE)


def test_nested_definitions_are_not_module_api():
    source = "def outer():\n    def inner():\n        pass\n"
    assert public_names(source) == {"outer"}


def test_unparseable_source_yields_nothing_rather_than_raising():
    """A syntax error is the linter's problem, not this check's."""
    assert public_names("def broken(:\n") == set()


# --- the regression itself ----------------------------------------------


def test_renaming_a_public_function_is_a_regression():
    after = BEFORE.replace("format_performance_table", "format_table")
    assert removed_public_names(BEFORE, after) == {"format_performance_table"}


def test_adding_alongside_existing_code_is_not_a_regression():
    after = BEFORE + "\n\ndef calculate_throughput(cards):\n    return 0\n"
    assert removed_public_names(BEFORE, after) == set()


def test_a_wholesale_rewrite_is_caught():
    """Story #7's actual failure: a new module with none of the old names."""
    after = "def calculate_throughput(cards):\n    return 0\n"
    removed = removed_public_names(BEFORE, after)
    assert "format_performance_table" in removed
    assert "calculate_cycle_time_and_lead_time" in removed


def test_changing_a_private_helper_is_allowed():
    after = BEFORE.replace("_parse_date", "_read_date")
    assert removed_public_names(BEFORE, after) == set()


# --- against a worktree --------------------------------------------------


def test_a_new_file_cannot_regress_anything(tmp_path):
    files = [Written(path="src/pkg/new.py", content="def f():\n    pass\n")]
    assert find_regressions(tmp_path, files) == {}


def test_an_existing_file_losing_names_is_reported(tmp_path):
    target = tmp_path / "src/pkg/mod.py"
    target.parent.mkdir(parents=True)
    target.write_text(BEFORE)
    files = [Written(path="src/pkg/mod.py", content="def other():\n    pass\n")]
    found = find_regressions(tmp_path, files)
    assert "format_performance_table" in found["src/pkg/mod.py"]


def test_non_python_files_are_not_checked(tmp_path):
    """Removing a heading from a README is not a regression."""
    target = tmp_path / "README.md"
    target.write_text("# Title\n\n## Section\n")
    files = [Written(path="README.md", content="# Title\n")]
    assert find_regressions(tmp_path, files) == {}


def test_preserving_everything_passes(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(BEFORE)
    files = [Written(path="mod.py", content=BEFORE + "\ndef extra():\n    pass\n")]
    assert find_regressions(tmp_path, files) == {}


# --- what the Developer is told -----------------------------------------


def test_the_message_names_the_file_and_the_lost_names():
    body = describe({"src/pkg/mod.py": {"format_performance_table", "Card"}})
    assert "src/pkg/mod.py" in body
    assert "format_performance_table" in body
    assert "Card" in body


def test_the_message_says_what_to_do_instead():
    body = describe({"m.py": {"f"}})
    assert "same name and signature" in body or "same names and signature" in body
    assert "separate story" in body
