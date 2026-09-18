"""Role boundaries that depend on granularity or altitude are enforced
structurally, because prompt wording does not hold them at 27B."""

from __future__ import annotations

import pytest

from crew_org.permissions import Capability, PermissionError_, Permissions


@pytest.fixture(scope="module")
def perms() -> Permissions:
    return Permissions.from_agents()


def test_product_owner_cannot_write_acceptance_criteria(perms):
    """Granularity boundary: the PO decomposes to epics and stops there."""
    assert not perms.allows("Product Owner", Capability.WRITE_ACCEPTANCE_CRITERIA)
    with pytest.raises(PermissionError_, match="may not write_acceptance_criteria"):
        perms.require("Product Owner", Capability.WRITE_ACCEPTANCE_CRITERIA)


def test_business_analyst_cannot_create_an_epic(perms):
    """The BA may disagree with an epic boundary but may not redraw it."""
    assert not perms.allows("Business Analyst", Capability.PROPOSE_EPIC)


def test_business_analyst_cannot_write_design(perms):
    """Altitude boundary: what, never how."""
    assert not perms.allows("Business Analyst", Capability.WRITE_DESIGN)


def test_qa_judges_behaviour_and_the_reviewer_judges_the_diff(perms):
    assert perms.allows("QA Engineer", Capability.VERDICT_BEHAVIOUR)
    assert not perms.allows("QA Engineer", Capability.VERDICT_DIFF)
    assert perms.allows("Code Reviewer", Capability.VERDICT_DIFF)
    assert not perms.allows("Code Reviewer", Capability.VERDICT_BEHAVIOUR)


def test_qa_cannot_fix_the_code_it_judges(perms):
    assert not perms.allows("QA Engineer", Capability.WRITE_CODE)


def test_developer_owns_its_own_documentation(perms):
    """Tech Writer was folded in: docs are part of Done, not a follow-up."""
    assert perms.allows("Developer", Capability.UPDATE_DOCS)


def test_scrum_master_narrates_and_nothing_more(perms):
    granted = {c for c in Capability if perms.allows("Scrum Master", c)}
    assert granted == {
        Capability.WRITE_STANDUP,
        Capability.WRITE_RETRO,
        Capability.FILE_PROCESS_DEFECT,
        Capability.COMMENT,
    }
    for forbidden in (Capability.WRITE_CODE, Capability.CREATE_STORY, Capability.ESTIMATE):
        assert not perms.allows("Scrum Master", forbidden)


def test_only_the_architect_writes_design(perms):
    writers = [r for r in perms.roles() if perms.allows(r, Capability.WRITE_DESIGN)]
    assert writers == ["Architect"]


def test_every_role_may_comment(perms):
    """Comments are the audit trail; no role is mute."""
    for role in perms.roles():
        assert perms.allows(role, Capability.COMMENT)


def test_unknown_role_is_refused_rather_than_silently_permitted(perms):
    with pytest.raises(PermissionError_, match="unknown role"):
        perms.require("Tech Writer", Capability.COMMENT)


def test_unknown_capability_in_config_fails_loudly():
    with pytest.raises(ValueError, match="unknown capability"):
        Permissions.from_agents({"x": {"role": "X", "can": ["teleport"]}})
