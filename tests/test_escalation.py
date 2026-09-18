"""The escalation rules are the load-bearing defence against escalation becoming
a crutch, so they are tested as rules rather than as plumbing."""

from __future__ import annotations

import pytest

from crew_org.config import load_org
from crew_org.escalation import (
    Disposition,
    EscalationLedger,
    EscalationPolicy,
    EscalationRecord,
    FailureClass,
    LocalFailure,
    utcnow,
)

REPAIRS = 2
BUDGET = 3
GOOD_JUSTIFICATION = "Requires a cross-cutting change to the GraphQL pagination layer."


@pytest.fixture
def policy() -> EscalationPolicy:
    return EscalationPolicy(
        never_escalate={FailureClass.SCHEMA, FailureClass.SCOPE},
        require_justification={FailureClass.CAPABILITY},
        local_repair_attempts=REPAIRS,
        sprint_budget=BUDGET,
    )


def failure(cls: FailureClass, *, attempts: int = 0, justification: str | None = None):
    return LocalFailure(
        card=42,
        role="Developer",
        failure_class=cls,
        attempts=attempts,
        detail="detail",
        justification=justification,
    )


# --- Classes that may never escalate ------------------------------------


def test_schema_failure_retries_locally_first(policy):
    d = policy.decide(failure(FailureClass.SCHEMA, attempts=0), spent=0)
    assert d.disposition is Disposition.RETRY_LOCAL
    assert not d.escalates


def test_schema_failure_becomes_a_prompt_defect_never_an_escalation(policy):
    d = policy.decide(failure(FailureClass.SCHEMA, attempts=REPAIRS), spent=0)
    assert d.disposition is Disposition.FILE_PROMPT_DEFECT
    assert not d.escalates


def test_scope_failure_always_returns_to_refinement(policy):
    # Even with budget free, repairs exhausted, and a justification attached.
    d = policy.decide(
        failure(FailureClass.SCOPE, attempts=99, justification=GOOD_JUSTIFICATION), spent=0
    )
    assert d.disposition is Disposition.RETURN_TO_REFINEMENT
    assert not d.escalates


@pytest.mark.parametrize("cls", [FailureClass.SCHEMA, FailureClass.SCOPE])
@pytest.mark.parametrize("attempts", [0, 1, 2, 10])
def test_forbidden_classes_never_escalate_under_any_conditions(policy, cls, attempts):
    d = policy.decide(failure(cls, attempts=attempts, justification=GOOD_JUSTIFICATION), spent=0)
    assert not d.escalates


# --- VERIFY --------------------------------------------------------------


def test_verify_repairs_locally_before_escalating(policy):
    assert policy.decide(failure(FailureClass.VERIFY, attempts=0), spent=0).disposition is (
        Disposition.RETRY_LOCAL
    )
    assert policy.decide(failure(FailureClass.VERIFY, attempts=1), spent=0).disposition is (
        Disposition.RETRY_LOCAL
    )


def test_verify_escalates_once_local_repair_is_exhausted(policy):
    d = policy.decide(failure(FailureClass.VERIFY, attempts=REPAIRS), spent=0)
    assert d.disposition is Disposition.ESCALATE


# --- CAPABILITY ----------------------------------------------------------


def test_capability_without_justification_is_treated_as_scope(policy):
    d = policy.decide(failure(FailureClass.CAPABILITY), spent=0)
    assert d.disposition is Disposition.RETURN_TO_REFINEMENT


def test_capability_with_a_hand_wave_is_treated_as_scope(policy):
    d = policy.decide(failure(FailureClass.CAPABILITY, justification="too hard"), spent=0)
    assert d.disposition is Disposition.RETURN_TO_REFINEMENT


def test_capability_with_justification_escalates_immediately(policy):
    # CAPABILITY needs no local repair first — the agent has declared reach, not failure.
    d = policy.decide(
        failure(FailureClass.CAPABILITY, attempts=0, justification=GOOD_JUSTIFICATION), spent=0
    )
    assert d.disposition is Disposition.ESCALATE


# --- Budget --------------------------------------------------------------


def test_exhausted_budget_blocks_rather_than_escalates(policy):
    d = policy.decide(
        failure(FailureClass.CAPABILITY, justification=GOOD_JUSTIFICATION), spent=BUDGET
    )
    assert d.disposition is Disposition.BLOCK


def test_final_budget_slot_is_usable(policy):
    d = policy.decide(
        failure(FailureClass.CAPABILITY, justification=GOOD_JUSTIFICATION), spent=BUDGET - 1
    )
    assert d.disposition is Disposition.ESCALATE


# --- Ledger --------------------------------------------------------------


def record(card: int, sprint: str = "S1") -> EscalationRecord:
    return EscalationRecord(
        at=utcnow(),
        sprint=sprint,
        card=card,
        role="Developer",
        failure_class=FailureClass.CAPABILITY,
        local_attempts=2,
        justification=GOOD_JUSTIFICATION,
        detail="detail",
    )


def test_ledger_charges_budget_per_card_not_per_attempt(tmp_path):
    ledger = EscalationLedger(tmp_path / "ledger.jsonl")
    ledger.record(record(1))
    ledger.record(record(1))  # same card escalated twice
    ledger.record(record(2))
    assert ledger.spent("S1") == 2


def test_ledger_scopes_spend_to_the_sprint(tmp_path):
    ledger = EscalationLedger(tmp_path / "ledger.jsonl")
    ledger.record(record(1, sprint="S1"))
    ledger.record(record(2, sprint="S2"))
    assert ledger.spent("S1") == 1
    assert ledger.spent("S2") == 1


def test_ledger_of_a_fresh_sprint_is_empty(tmp_path):
    assert EscalationLedger(tmp_path / "ledger.jsonl").spent("S1") == 0


# --- The real config -----------------------------------------------------


def test_shipped_org_config_builds_a_policy_that_forbids_schema_and_scope():
    policy = EscalationPolicy.from_config(load_org())
    assert policy.never_escalate == {FailureClass.SCHEMA, FailureClass.SCOPE}
    for cls in (FailureClass.SCHEMA, FailureClass.SCOPE):
        assert not policy.decide(failure(cls, attempts=99), spent=0).escalates
