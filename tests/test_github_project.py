"""The board client is the crew's only route to its own state, so parsing and
failure reporting are tested against recorded response shapes."""

from __future__ import annotations

import json

import httpx
import pytest

from crew_org.tools.github_project import (
    BoardError,
    PermissionDenied,
    ProjectClient,
    _to_card,
)

OWNER, NUMBER = "mqucifer", 1

FIELDS_RESPONSE = {
    "data": {
        "organization": {
            "projectV2": {
                "id": "PVT_1",
                "title": "Crew Delivery",
                "fields": {
                    "nodes": [
                        {"id": "F_title", "name": "Title", "dataType": "TITLE"},
                        {
                            "id": "F_status",
                            "name": "Status",
                            "dataType": "SINGLE_SELECT",
                            "options": [
                                {"id": "o_inbox", "name": "Inbox (Goals)"},
                                {"id": "o_ready", "name": "Ready"},
                                {"id": "o_prog", "name": "In Progress"},
                            ],
                        },
                        {"id": "F_points", "name": "Points", "dataType": "NUMBER"},
                        {
                            "id": "F_sprint",
                            "name": "Sprint",
                            "dataType": "ITERATION",
                            "configuration": {
                                "iterations": [
                                    {"id": "it_s1", "title": "S1"},
                                    {"id": "it_s2", "title": "S2"},
                                ]
                            },
                        },
                        {},  # a null-ish node, as the API sometimes returns
                    ]
                },
            }
        }
    }
}


def item(number: int, status: str | None = None, labels: list[str] | None = None) -> dict:
    values = []
    if status:
        values.append({"name": status, "field": {"name": "Status"}})
    return {
        "id": f"ITEM_{number}",
        "fieldValues": {"nodes": values},
        "content": {
            "number": number,
            "title": f"Card {number}",
            "url": f"https://github.com/mqucifer/crew/issues/{number}",
            "state": "OPEN",
            "repository": {"name": "crew"},
            "labels": {"nodes": [{"name": n} for n in (labels or [])]},
        },
    }


def items_response(nodes: list[dict], *, cursor: str | None = None) -> dict:
    return {
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def client_for(*responses: dict) -> ProjectClient:
    """A client whose GraphQL calls return the given bodies in order."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    return ProjectClient("tok", OWNER, NUMBER, client=httpx.Client(transport=transport))


# --- schema --------------------------------------------------------------


def test_schema_reads_fields_and_option_ids():
    schema = client_for(FIELDS_RESPONSE).schema
    assert schema.project_id == "PVT_1"
    assert schema.field("Status").id == "F_status"
    assert schema.option_id("Status", "Ready") == "o_ready"


def test_iteration_titles_resolve_like_options():
    schema = client_for(FIELDS_RESPONSE).schema
    assert schema.option_id("Sprint", "S1") == "it_s1"


def test_unknown_field_error_lists_what_exists():
    schema = client_for(FIELDS_RESPONSE).schema
    with pytest.raises(BoardError, match="no field named 'Stage'"):
        schema.field("Stage")


def test_unknown_option_error_lists_what_exists():
    schema = client_for(FIELDS_RESPONSE).schema
    with pytest.raises(BoardError) as exc:
        schema.option_id("Status", "Shipped")
    assert "In Progress" in str(exc.value)


# --- cards ---------------------------------------------------------------


def test_cards_flatten_field_values_and_labels():
    c = client_for(items_response([item(7, "Ready", ["crew:dev", "blocked"])]))
    card = c.cards()[0]
    assert (card.number, card.status, card.repo) == (7, "Ready", "crew")
    assert card.labels == frozenset({"crew:dev", "blocked"})
    assert card.is_blocked


def test_draft_items_are_skipped():
    """Every card must be a real issue — drafts have no number and no audit trail."""
    draft = {"id": "ITEM_draft", "fieldValues": {"nodes": []}, "content": {}}
    assert client_for(items_response([draft, item(1)])).cards() == [
        c for c in client_for(items_response([item(1)])).cards()
    ]


def test_pagination_is_followed():
    page1 = items_response([item(1), item(2)], cursor="CUR")
    page2 = items_response([item(3)])
    c = client_for(page1, page2)
    assert [card.number for card in c.cards()] == [1, 2, 3]


def test_counts_group_by_status_column():
    c = client_for(
        items_response([item(1, "Ready"), item(2, "Ready"), item(3, "In Progress"), item(4)])
    )
    assert c.counts() == {"Ready": 2, "In Progress": 1}


def test_needs_human_is_read_from_labels():
    card = _to_card(item(9, "Inbox (Goals)", ["needs:human"]))
    assert card is not None and card.needs_human


# --- failures ------------------------------------------------------------


def test_permission_error_points_at_the_auth_doc():
    body = {"errors": [{"message": "Resource not accessible by personal access token"}]}
    with pytest.raises(PermissionDenied, match="agent-auth"):
        _ = client_for(body).schema


def test_invisible_board_explains_the_ownership_requirement():
    with pytest.raises(BoardError, match="organization-owned"):
        _ = client_for({"data": {"organization": None}}).schema


def test_rejected_token_is_named_as_such():
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    c = ProjectClient(
        "bad", OWNER, NUMBER, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(PermissionDenied, match="crew auth"):
        _ = c.schema


# --- writes --------------------------------------------------------------


def test_set_status_sends_the_resolved_option_id():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        if "fields(first" in payload["query"]:
            return httpx.Response(200, json=FIELDS_RESPONSE)
        return httpx.Response(200, json={"data": {"organization": {"projectV2": {"id": "PVT_1"}}}})

    c = ProjectClient(
        "tok", OWNER, NUMBER, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    c.set_status("ITEM_7", "In Progress")
    assert sent[-1]["variables"]["option"] == "o_prog"
    assert sent[-1]["variables"]["item"] == "ITEM_7"


def test_moving_to_a_column_that_does_not_exist_fails_before_the_call():
    c = client_for(FIELDS_RESPONSE)
    with pytest.raises(BoardError, match="no option"):
        c.set_status("ITEM_7", "Shipped")


# --- WIP counts ----------------------------------------------------------


def typed(number: int, status: str, work_type: str) -> dict:
    node = item(number, status)
    node["fieldValues"]["nodes"].append({"name": work_type, "field": {"name": "Work Type"}})
    return node


def test_containers_do_not_consume_wip():
    """A Goal or Epic sitting in a column tracks its children; counting it would
    let a decomposition exhaust a WIP limit with no work started."""
    c = client_for(
        items_response(
            [
                typed(1, "Ready", "Goal"),
                typed(2, "Ready", "Epic"),
                typed(3, "Ready", "Story"),
                typed(4, "Ready", "Bug"),
            ]
        )
    )
    assert c.counts() == {"Ready": 2}


def test_counts_can_be_computed_from_cards_already_read():
    """Saves a second round trip during a tick."""
    c = client_for(items_response([typed(1, "Ready", "Story")]))
    cards = c.cards()
    assert c.counts(cards) == {"Ready": 1}
