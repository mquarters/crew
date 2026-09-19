"""The scorecard is read off the board, not written by hand."""

from __future__ import annotations

from crew_org.flows.capability import LADDER, scorecard
from crew_org.tools.github_project import Card


def card(
    number, *, repo="crew", capability=None, status="Ready", state="OPEN", points=3, work="Story"
):
    return Card(
        item_id=f"I{number}",
        number=number,
        title=f"Card {number}",
        status=status,
        state=state,
        work_type=work,
        repo=repo,
        points=points,
        capability=capability,
    )


def test_every_capability_has_a_row_even_with_no_cards():
    """An absent capability is the finding. A row that vanishes because nothing
    carries it is the one row you needed to see."""
    out = scorecard([], crew_repo="crew")

    assert [r.capability for r in out.rows] == list(LADDER)
    assert all(r.total == 0 for r in out.rows)


def test_landed_and_in_flight_are_counted_apart():
    out = scorecard(
        [
            card(1, capability="Review", status="Done", points=5),
            card(2, capability="Review", points=2),
        ],
        crew_repo="crew",
    )
    row = next(r for r in out.rows if r.capability == "Review")

    assert (row.done, row.open) == (1, 1)
    assert (row.points_done, row.points_open) == (5, 2)


def test_a_closed_card_counts_as_landed_wherever_it_sits():
    """Nine closed cards were sitting outside Done when this was written."""
    out = scorecard(
        [card(1, capability="Release", status="Ready", state="CLOSED")], crew_repo="crew"
    )

    assert next(r for r in out.rows if r.capability == "Release").done == 1


def test_a_product_card_climbs_the_other_ladder():
    """A card in a delivery repository advances a product, not the crew's
    ability to run a process. Counting it as the latter makes the scorecard read
    healthier than it is."""
    out = scorecard(
        [card(1, repo="sprint-metrics", capability="Release"), card(2, capability="Release")],
        crew_repo="crew",
    )

    assert out.product_open == 1
    assert next(r for r in out.rows if r.capability == "Release").open == 1, "only the crew card"


def test_an_unattributed_crew_card_is_named():
    """The one thing that makes the whole picture a lie."""
    out = scorecard([card(7), card(9, capability="Planning")], crew_repo="crew")

    assert out.unattributed == [7]


def test_an_unattributed_card_that_already_landed_is_not_chased():
    """Nothing is gained by asking for an attribution on finished work."""
    out = scorecard([card(7, status="Done")], crew_repo="crew")

    assert out.unattributed == []


def test_an_epic_is_not_a_card_that_climbs():
    """Goals and epics are containers. Counting them double-counts their
    stories."""
    out = scorecard([card(1, work="Epic", capability="Planning")], crew_repo="crew")

    assert all(r.total == 0 for r in out.rows)
    assert out.unattributed == []


def test_a_capability_nobody_chose_is_ignored_rather_than_invented():
    """The board's options can change under us; an unknown one must not create
    a row that is not on the ladder."""
    out = scorecard([card(1, capability="Something else")], crew_repo="crew")

    assert [r.capability for r in out.rows] == list(LADDER)
    assert out.unattributed == [1]
