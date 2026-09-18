"""The constitution's story rules are validators, so a malformed story is a
SCHEMA failure the repair loop handles rather than something a human notices
three columns later."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crew_org.crews.refinement_crew import (
    MAX_EPICS,
    MIN_EPICS,
    POINT_SCALE,
    AcceptanceCriterion,
    Epic,
    EpicProposal,
    Story,
    StoryProposal,
)


def criteria(n: int) -> list[AcceptanceCriterion]:
    return [AcceptanceCriterion(given=f"g{i}", when=f"w{i}", then=f"t{i}") for i in range(n)]


def story(**overrides) -> Story:
    params = dict(
        title="Report cycle time",
        as_a="Sponsor",
        i_want="cycle time per sprint",
        so_that="I can see how quickly work moves",
        acceptance_criteria=criteria(2),
        points=3,
    )
    params.update(overrides)
    return Story(**params)


@pytest.mark.parametrize("points", POINT_SCALE)
def test_every_point_on_the_scale_is_accepted(points):
    assert story(points=points).points == points


@pytest.mark.parametrize("points", [0, 4, 6, 7, 13, 21])
def test_estimates_off_the_scale_are_rejected(points):
    with pytest.raises(ValidationError, match="estimation scale"):
        story(points=points)


def test_thirteen_points_is_rejected_because_it_must_be_split():
    """§6: nothing larger than 8 enters a sprint."""
    with pytest.raises(ValidationError, match="must be split"):
        story(points=13)


def test_a_single_acceptance_criterion_is_not_enough():
    with pytest.raises(ValidationError, match="at least 2 acceptance criteria"):
        story(acceptance_criteria=criteria(1))


def test_no_acceptance_criteria_is_rejected():
    with pytest.raises(ValidationError):
        story(acceptance_criteria=[])


def test_the_edge_case_requirement_is_stated_in_the_error():
    """The message has to teach, because the model reads it on retry."""
    with pytest.raises(ValidationError, match="failure or edge case"):
        story(acceptance_criteria=criteria(1))


def test_an_empty_proposal_is_rejected():
    with pytest.raises(ValidationError, match="at least 2 epics"):
        EpicProposal(epics=[], ordering_rationale="none")


def test_an_epic_must_decompose_into_at_least_one_story():
    with pytest.raises(ValidationError, match="at least one story"):
        StoryProposal(epic_title="E", stories=[])


def epic(title: str = "Report performance", **overrides) -> Epic:
    params = dict(
        title=title,
        outcome="Sponsor sees metrics",
        rationale="why",
        separately_deliverable="The Sponsor can read the metrics with nothing else built.",
    )
    params.update(overrides)
    return Epic(**params)


def test_a_well_formed_proposal_validates():
    proposal = EpicProposal(epics=[epic(), epic("Emit JSON")], ordering_rationale="value first")
    assert proposal.epics[0].title == "Report performance"


# --- decomposition, encoded ---------------------------------------------


def test_one_epic_is_not_a_decomposition():
    """Observed in practice: the same goal produced two epics on one run and one
    on the next. The judgement is encoded rather than left to the Sponsor."""
    with pytest.raises(ValidationError, match="not a decomposition"):
        EpicProposal(epics=[epic()], ordering_rationale="only one")


def test_too_many_epics_is_also_rejected():
    many = [epic(f"Epic {i}") for i in range(MAX_EPICS + 1)]
    with pytest.raises(ValidationError, match="too many"):
        EpicProposal(epics=many, ordering_rationale="lots")


def test_the_allowed_range_is_accepted():
    for n in range(MIN_EPICS, MAX_EPICS + 1):
        proposal = EpicProposal(
            epics=[epic(f"Epic {i}") for i in range(n)], ordering_rationale="ok"
        )
        assert len(proposal.epics) == n


def test_duplicate_titles_are_rejected():
    with pytest.raises(ValidationError, match="share a title"):
        EpicProposal(epics=[epic("Same"), epic("same")], ordering_rationale="x")


# --- standing alone ------------------------------------------------------


def test_a_hand_wave_is_not_a_standalone_argument():
    with pytest.raises(ValidationError, match="what a user could do"):
        epic(separately_deliverable="it is useful")


def test_restating_the_outcome_is_not_an_argument():
    """Why a slice stands alone is a different question from what it delivers."""
    with pytest.raises(ValidationError, match="repeats the outcome"):
        epic(
            outcome="The Sponsor can read every metric in a table",
            separately_deliverable="The Sponsor can read every metric in a table",
        )


def test_a_real_standalone_argument_is_accepted():
    assert epic(
        separately_deliverable="The Sponsor can read every metric in the terminal alone."
    ).separately_deliverable
