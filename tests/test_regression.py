"""Detecting an implementation that destroys existing work.

A model asked to add one metric will happily redesign the module it is adding
to. Observed on story #7, which rewrote story #6's merged code and renamed its
public functions; and again on story #9, which turned three properties into
methods and left thirteen merged tests failing.

This is checked mechanically because a prompt asking the model not to do it is a
request, not a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from crew_org.tools.regression import (
    broken_contracts,
    describe,
    describe_contracts,
    find_regressions,
    public_names,
    removed_public_names,
    signatures_for_context,
    undeclared_changes,
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
    assert "format_performance_table" in found["src/pkg/mod.py"]["removed"]


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
    body = describe(
        {"src/pkg/mod.py": {"removed": {"format_performance_table", "Card"}, "altered": set()}}
    )
    assert "src/pkg/mod.py" in body
    assert "format_performance_table" in body
    assert "Card" in body


def test_the_message_says_what_to_do_instead():
    body = describe({"m.py": {"removed": {"f"}, "altered": set()}})
    assert "same signature, same body" in body
    assert "modifies" in body


# --- silent edits, not just deletions ------------------------------------


def test_a_changed_body_with_the_same_name_is_caught():
    """A rewrite that keeps every name can still replace every body."""
    after = BEFORE.replace('    return ""', '    return "something else entirely"')
    assert undeclared_changes(BEFORE, after, declared=set()) == {"format_performance_table"}


def test_a_changed_signature_is_caught():
    after = BEFORE.replace(
        "def calculate_cycle_time_and_lead_time(card):",
        "def calculate_cycle_time_and_lead_time(card, *, units):",
    )
    assert undeclared_changes(BEFORE, after, declared=set()) == {
        "calculate_cycle_time_and_lead_time"
    }


def test_a_declared_change_is_allowed():
    """Changing existing code is legitimate when it is deliberate and named."""
    after = BEFORE.replace('    return ""', '    return "with throughput"')
    declared = {"format_performance_table"}
    assert undeclared_changes(BEFORE, after, declared) == set()


def test_adding_something_new_is_not_a_change():
    after = BEFORE + "\n\ndef calculate_throughput(cards):\n    return 0\n"
    assert undeclared_changes(BEFORE, after, declared=set()) == set()


def test_reformatting_a_preserved_definition_still_counts_as_a_change():
    """Byte-for-byte is the standard: incidental rewrites are how drift enters."""
    after = BEFORE.replace(
        "def format_performance_table(cards):", "def format_performance_table(\n    cards,\n):"
    )
    assert "format_performance_table" in undeclared_changes(BEFORE, after, declared=set())


def test_private_helpers_are_not_policed():
    """How a module organises itself internally is the author's business."""
    after = BEFORE.replace(
        "def _parse_date(value):\n    return None",
        "def _parse_date(value):\n    return value or None",
    )
    assert undeclared_changes(BEFORE, after, declared=set()) == set()


def test_an_undeclared_edit_is_reported_against_the_file(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(BEFORE)
    changed = BEFORE.replace('    return ""', '    return "drifted"')
    found = find_regressions(tmp_path, [Written(path="mod.py", content=changed)])
    assert found["mod.py"]["altered"] == {"format_performance_table"}


def test_declaring_the_edit_clears_it(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(BEFORE)
    changed = BEFORE.replace('    return ""', '    return "declared"')
    found = find_regressions(
        tmp_path, [Written(path="mod.py", content=changed)], declared={"format_performance_table"}
    )
    assert found == {}


# --- contracts other code already depends on -----------------------------
#
# Story #9 is the case these are written from: asked to add one column, the
# Developer changed a frozen dataclass, turned three properties into methods,
# changed two return types from `int` to `float | None`, and gave
# format_performance_table five scalar arguments in place of two. The private
# helper consuming those values was never touched, so it summed integers over a
# list of None and thirteen merged tests died of a TypeError.

MODULE = """
from dataclasses import dataclass


@dataclass(frozen=True)
class Card:
    created: date
    started: date | None = None

    @property
    def cycle_time(self) -> int:
        return 0


def format_performance_table(cards, wip_limits=None) -> str:
    return "table"


def _mean_days(values):
    return sum(values) / len(values)
"""


@dataclass
class Ed:
    path: str
    operation: str
    target: str
    source: str = ""


@pytest.fixture
def module(tmp_path):
    (tmp_path / "m.py").write_text(MODULE)
    return tmp_path


def test_rewriting_a_body_is_allowed(module):
    """Stories are made of body edits. A check that refused them refuses
    everything."""
    edit = Ed(
        "m.py",
        "replace",
        "format_performance_table",
        "def format_performance_table(cards, wip_limits=None) -> str:\n"
        '    return "a table with one more column"\n',
    )
    assert broken_contracts(module, [edit]) == {}


def test_changing_a_parameter_list_is_refused(module):
    edit = Ed(
        "m.py",
        "replace",
        "format_performance_table",
        "def format_performance_table(cycle_time, lead_time, throughput) -> str:\n"
        '    return "table"\n',
    )
    broken = broken_contracts(module, [edit])
    assert "m.py::format_performance_table" in broken
    was, now = broken["m.py::format_performance_table"]
    assert "cards, wip_limits" in was
    assert "cycle_time" in now


def test_a_property_becoming_a_method_is_refused(module):
    """The exact change that broke story #9: callers write `card.cycle_time`,
    and afterwards they must write `card.cycle_time()`."""
    edit = Ed(
        "m.py",
        "replace",
        "Card.cycle_time",
        "def cycle_time(self) -> float | None:\n    return None\n",
    )
    broken = broken_contracts(module, [edit])
    assert "m.py::Card.cycle_time" in broken
    was, now = broken["m.py::Card.cycle_time"]
    assert "property" in was and "property" not in now


def test_adding_a_field_to_a_dataclass_is_refused(module):
    """A dataclass's fields are its constructor, so a new one is a new call."""
    edit = Ed(
        "m.py",
        "replace",
        "Card",
        "@dataclass\nclass Card:\n    title: str\n    created: date\n"
        "    started: date | None = None\n",
    )
    assert "m.py::Card" in broken_contracts(module, [edit])


def test_deleting_a_public_definition_is_refused(module):
    broken = broken_contracts(module, [Ed("m.py", "delete", "format_performance_table")])
    assert broken["m.py::format_performance_table"][1] == "removed"


def test_adding_a_new_definition_is_not_a_contract_break(module):
    edit = Ed(
        "m.py",
        "add",
        "calculate_blocked_aging",
        "def calculate_blocked_aging(cards):\n    return 0\n",
    )
    assert broken_contracts(module, [edit]) == {}


def test_a_private_helper_has_no_contract_to_break(module):
    """`_mean_days` is the author's business. Only names other code can import
    are promises."""
    edit = Ed(
        "m.py", "replace", "_mean_days", "def _mean_days(values, default=0):\n    return default\n"
    )
    assert broken_contracts(module, [edit]) == {}


def test_a_target_that_does_not_exist_is_not_reported_here(module):
    """Inventing a name is a different failure, and the edit machinery already
    reports it as one. Reporting it twice would spend two attempts on it."""
    assert broken_contracts(module, [Ed("m.py", "replace", "no_such_thing", "def x(): pass")]) == {}


def test_the_message_names_the_definition_and_both_shapes(module):
    edit = Ed(
        "m.py",
        "replace",
        "format_performance_table",
        "def format_performance_table(a) -> str:\n    return ''\n",
    )
    message = describe_contracts(broken_contracts(module, [edit]))
    assert "format_performance_table" in message
    assert "cards, wip_limits" in message
    assert "add a new definition alongside" in message


def test_the_context_shows_what_the_check_will_judge(module):
    """The model is told the contract before it writes, not only after it has
    broken one. A constraint never stated is not one the model can respect."""
    signatures = signatures_for_context(MODULE)
    assert signatures["format_performance_table"] == "(cards, wip_limits)"
    assert signatures["Card.cycle_time"] == "(self) [property]"
    assert "_mean_days" not in signatures
