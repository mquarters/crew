"""The never-push-to-main rail is enforced here in code because branch
protection needs GitHub Pro on a private repo. Until that changes, these tests
are the rail."""

from __future__ import annotations

import pytest

from crew_org.git_ops import (
    BranchNameError,
    ProtectedBranchError,
    assert_writable,
    branch_name,
    validate_branch_name,
)


@pytest.mark.parametrize("branch", ["main", "master", "trunk", "release", "develop"])
def test_protected_branches_are_refused(branch):
    with pytest.raises(ProtectedBranchError, match="protected"):
        assert_writable(branch)


@pytest.mark.parametrize("branch", ["MAIN", " main ", "Master"])
def test_protection_is_not_defeated_by_case_or_whitespace(branch):
    with pytest.raises(ProtectedBranchError):
        assert_writable(branch)


def test_a_feature_branch_is_writable():
    assert_writable("feat/42-cycle-time-metric")  # must not raise


def test_branch_name_follows_the_convention():
    assert branch_name(42, "Cycle time metric") == "feat/42-cycle-time-metric"
    assert branch_name(7, "Fix off-by-one", kind="fix") == "fix/7-fix-off-by-one"


def test_branch_name_slugifies_punctuation():
    assert branch_name(3, "Report: cycle time (p50/p90)") == "feat/3-report-cycle-time-p50-p90"


def test_unknown_branch_type_is_refused():
    with pytest.raises(BranchNameError, match="not one of"):
        branch_name(1, "thing", kind="hotfix")


def test_nonsense_issue_number_is_refused():
    with pytest.raises(BranchNameError, match="positive"):
        branch_name(0, "thing")


def test_summary_that_slugifies_to_nothing_is_refused():
    with pytest.raises(BranchNameError, match="empty slug"):
        branch_name(1, "!!!")


def test_valid_branch_names_pass_validation():
    for branch in ("feat/1-a", "fix/22-two-words", "spike/9-investigate-pagination"):
        validate_branch_name(branch)


@pytest.mark.parametrize(
    "branch",
    ["feat-42-no-slash", "feat/no-issue-number", "42-missing-type", "feat/42-Has-Capitals"],
)
def test_malformed_branch_names_are_refused(branch):
    with pytest.raises(BranchNameError):
        validate_branch_name(branch)


def test_validation_checks_protection_before_shape():
    """'main' is refused as protected, not merely as badly shaped."""
    with pytest.raises(ProtectedBranchError):
        validate_branch_name("main")
