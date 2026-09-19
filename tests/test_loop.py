"""A tick runs the whole loop, not a quarter of it."""

from __future__ import annotations

import pytest

from crew_org.events import EventSink
from crew_org.flows import loop


@pytest.fixture
def crew():
    """A Crew whose every dependency is unused: the phases are all faked."""
    return loop.Crew(
        board=None,
        issues=None,
        sink=EventSink(None),
        ws=None,
        sandbox=None,
        rules=None,
        policy=None,
        ledger=None,
        org={},
        repo="sprint-metrics",
        repos={"sprint-metrics"},
        sprint="S1",
        capacity=20,
        reviewer=None,
        reviewer_login="approver[bot]",
    )


def phases(monkeypatch, *specs):
    """Replace the phase table with `(name, moved-or-raises)` pairs."""
    calls: list[str] = []

    def make(name, behaviour):
        def run(_crew, *, dry_run):
            calls.append(name)
            if isinstance(behaviour, Exception):
                raise behaviour
            moved = behaviour.pop(0) if isinstance(behaviour, list) else behaviour
            return loop.PhaseOutcome(name, moved=moved, summary=f"{name} ran")

        return (name, run)

    monkeypatch.setattr(loop, "PHASES", tuple(make(n, b) for n, b in specs))
    return calls


def test_every_phase_runs_in_dependency_order(crew, monkeypatch):
    """Drain first: work already started is pushed forward before new work is
    claimed, so a story does not branch from a main missing its predecessors."""
    calls = phases(
        monkeypatch,
        ("refine", False),
        ("admit", False),
        ("review", False),
        ("qa", False),
        ("deliver", False),
    )
    loop.run(crew)

    assert calls == ["refine", "admit", "review", "qa", "deliver"]


def test_it_keeps_going_while_anything_moves(crew, monkeypatch):
    """AC1: until nothing further can move. Delivery moving means refinement
    may have something new to do, so quiescence is the only stopping point."""
    calls = phases(monkeypatch, ("refine", [True, True, False]), ("deliver", False))
    result = loop.run(crew)

    assert result.passes == 3, "two passes that moved, then one that did not"
    assert calls.count("refine") == 3


def test_a_pass_that_moves_nothing_ends_the_tick(crew, monkeypatch):
    phases(monkeypatch, ("refine", False))
    assert loop.run(crew).passes == 1


def test_a_board_that_will_not_settle_is_capped(crew, monkeypatch):
    """A pass that keeps moving forever is a bug, not a busy board."""
    phases(monkeypatch, ("refine", True))
    assert loop.run(crew, max_passes=3).passes == 3


def test_a_failing_phase_does_not_abort_the_pass(crew, monkeypatch):
    """AC2: later phases still run for the cards they can act on."""
    calls = phases(
        monkeypatch,
        ("refine", RuntimeError("model unavailable")),
        ("qa", False),
        ("deliver", False),
    )
    result = loop.run(crew)

    assert calls == ["refine", "qa", "deliver"], "the pass continued"
    assert [o.name for o in result.failed] == ["refine"]
    assert "model unavailable" in result.failed[0].error


def test_a_failure_is_reported_rather_than_raised(crew, monkeypatch):
    """A tick that aborts on the first error leaves the board partway through a
    state nobody chose."""
    phases(monkeypatch, ("deliver", RuntimeError("github is down")))
    result = loop.run(crew)

    assert result.failed, "recorded"
    assert result.last("deliver").error.startswith("RuntimeError")
    assert not result.moved


def test_a_failing_phase_alone_does_not_keep_the_loop_spinning(crew, monkeypatch):
    """A phase that fails every pass has not moved anything, so it must not
    read as progress — that is an infinite loop wearing a failure's clothes."""
    phases(monkeypatch, ("refine", RuntimeError("still down")))
    assert loop.run(crew).passes == 1
