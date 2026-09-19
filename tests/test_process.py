"""WIP limits and card movement are code precisely so they cannot be talked
past, so the refusals are what get tested."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crew_org.config import load_org
from crew_org.process import ProcessRules, Refusal

COLUMNS = ["Inbox (Goals)", "Needs Refinement", "Ready", "In Progress", "Awaiting QA", "Done"]


@pytest.fixture
def rules() -> ProcessRules:
    return ProcessRules(
        columns=COLUMNS,
        blocked_column="Blocked",
        human_gates={"Inbox (Goals)"},
        wip_limits={"In Progress": 3},
        blocked_aging_days=3,
    )


def test_forward_one_step_is_allowed(rules):
    assert rules.may_move(frm="Ready", to="In Progress", counts={}).allowed


def test_work_may_not_jump_a_gate(rules):
    v = rules.may_move(frm="Ready", to="Done", counts={})
    assert not v.allowed
    assert v.refusal is Refusal.SKIPS_COLUMNS
    assert "In Progress" in v.reason


def test_rework_backwards_is_always_allowed(rules):
    assert rules.may_move(frm="Awaiting QA", to="In Progress", counts={}).allowed
    assert rules.may_move(frm="Awaiting QA", to="Needs Refinement", counts={}).allowed


def test_agents_cannot_move_a_card_out_of_a_human_gate(rules):
    v = rules.may_move(frm="Inbox (Goals)", to="Needs Refinement", counts={})
    assert not v.allowed
    assert v.refusal is Refusal.HUMAN_GATE


def test_the_sponsor_can_move_a_card_out_of_a_human_gate(rules):
    assert rules.may_move(
        frm="Inbox (Goals)", to="Needs Refinement", counts={}, by_agent=False
    ).allowed


def test_blocking_is_reachable_from_anywhere_in_the_flow(rules):
    for column in COLUMNS:
        if column in rules.human_gates:
            continue
        assert rules.may_move(frm=column, to="Blocked", counts={}).allowed


def test_a_card_at_a_human_gate_cannot_even_be_blocked(rules):
    """The gate is absolute: a card awaiting the Sponsor is not an agent's to
    move anywhere, including sideways into Blocked."""
    v = rules.may_move(frm="Inbox (Goals)", to="Blocked", counts={})
    assert not v.allowed
    assert v.refusal is Refusal.HUMAN_GATE


def test_unblocking_returns_to_the_flow_without_a_skip_complaint(rules):
    assert rules.may_move(frm="Blocked", to="Awaiting QA", counts={}).allowed


def test_a_full_column_refuses_new_work(rules):
    v = rules.may_move(frm="Ready", to="In Progress", counts={"In Progress": 3})
    assert not v.allowed
    assert v.refusal is Refusal.WIP_LIMIT
    assert "Finish the oldest card" in v.reason


def test_a_column_below_its_limit_accepts_work(rules):
    assert rules.may_move(frm="Ready", to="In Progress", counts={"In Progress": 2}).allowed


def test_wip_limits_apply_to_rework_too(rules):
    """Otherwise a full column could be refilled from the far side."""
    v = rules.may_move(frm="Awaiting QA", to="In Progress", counts={"In Progress": 3})
    assert not v.allowed
    assert v.refusal is Refusal.WIP_LIMIT


def test_unknown_columns_are_refused_not_guessed(rules):
    assert rules.may_move(frm="Ready", to="Shipped", counts={}).refusal is Refusal.UNKNOWN_COLUMN


def test_over_limit_reports_actual_breaches(rules):
    assert rules.over_limit({"In Progress": 5}) == {"In Progress": (5, 3)}
    assert rules.over_limit({"In Progress": 3}) == {}


def test_aging_blocked_cards_are_surfaced(rules):
    now = datetime.now(UTC)
    aging = rules.aging_blocked({1: now - timedelta(days=5), 2: now - timedelta(days=1)}, now=now)
    assert aging == {1: 5}


def test_shipped_config_builds_working_rules():
    rules = ProcessRules.from_config(load_org())
    assert rules.may_move(frm="Ready", to="Sprint Backlog", counts={}).allowed
    assert not rules.may_move(frm="Inbox (Goals)", to="Ready", counts={}).allowed
