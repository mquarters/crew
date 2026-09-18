"""Projects v2 client — the board's read/write transport.

Deliberately a dumb transport. It knows how to read the board and how to change
a field; it knows nothing about whether a change is *allowed*. Legality lives in
crew_org.process, so that the rules stay in one testable place rather than being
re-implemented at every call site.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

import httpx
from pydantic import BaseModel, Field

GRAPHQL = "https://api.github.com/graphql"
TIMEOUT = 30.0
PAGE_SIZE = 50


class BoardError(RuntimeError):
    """A GraphQL call failed."""


class PermissionDenied(BoardError):
    """The token cannot do this. Almost always a missing grant, not a bug."""


class BoardField(BaseModel):
    id: str
    name: str
    data_type: str
    # Single-select option name -> option id; iteration title -> iteration id.
    options: dict[str, str] = Field(default_factory=dict)


class BoardSchema(BaseModel):
    project_id: str
    title: str
    fields: dict[str, BoardField]

    def field(self, name: str) -> BoardField:
        try:
            return self.fields[name]
        except KeyError:
            known = ", ".join(sorted(self.fields))
            raise BoardError(f"no field named {name!r} on this board. Fields: {known}") from None

    def option_id(self, field: str, option: str) -> str:
        f = self.field(field)
        try:
            return f.options[option]
        except KeyError:
            known = ", ".join(sorted(f.options))
            raise BoardError(f"{field!r} has no option {option!r}. Options: {known}") from None


class Card(BaseModel):
    """One board item, flattened into the shape the crew reasons about."""

    item_id: str
    number: int | None = None
    title: str = ""
    url: str | None = None
    repo: str | None = None
    state: str | None = None
    status: str | None = None
    work_type: str | None = None
    priority: str | None = None
    owner_agent: str | None = None
    sprint: str | None = None
    points: float | None = None
    escalations: float | None = None
    labels: frozenset[str] = frozenset()

    @property
    def is_blocked(self) -> bool:
        return "blocked" in self.labels

    @property
    def needs_human(self) -> bool:
        return "needs:human" in self.labels


_FIELDS_QUERY = """
query($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      title
      fields(first: 50) {
        nodes {
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { id name options { id name } }
          ... on ProjectV2IterationField {
            id name configuration { iterations { id title } }
          }
        }
      }
    }
  }
}
"""

_ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  organization(login: $owner) {
    projectV2(number: $number) {
      items(first: {page_size}, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldNumberValue {
                number field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
          content {
            ... on Issue {
              number title url state
              repository { name }
              labels(first: 20) { nodes { name } }
            }
          }
        }
      }
    }
  }
}
""".replace("{page_size}", str(PAGE_SIZE))

_SET_SELECT = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
}
"""

_SET_NUMBER = """
mutation($project: ID!, $item: ID!, $field: ID!, $value: Float!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field, value: {number: $value}
  }) { projectV2Item { id } }
}
"""

_SET_ITERATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $iteration: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {iterationId: $iteration}
  }) { projectV2Item { id } }
}
"""

_ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""

_DELETE_ITEM = """
mutation($project: ID!, $item: ID!) {
  deleteProjectV2Item(input: {projectId: $project, itemId: $item}) { deletedItemId }
}
"""

# Field name on the board -> attribute on Card.
_FIELD_TO_ATTR = {
    "Status": "status",
    "Work Type": "work_type",
    "Priority": "priority",
    "Owner Agent": "owner_agent",
    "Sprint": "sprint",
    "Points": "points",
    "Escalations": "escalations",
}


class ProjectClient:
    def __init__(self, token: str, owner: str, number: int, *, client: httpx.Client | None = None):
        self.owner = owner
        self.number = number
        self._client = client or httpx.Client(
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )

    # --- transport ------------------------------------------------------
    def _call(self, query: str, **variables: Any) -> dict[str, Any]:
        """Execute a query or mutation and return its `data`.

        Mutations and queries return different top-level shapes, so unwrapping
        the project is deliberately *not* done here — see `_project`.
        """
        response = self._client.post(GRAPHQL, json={"query": query, "variables": variables})
        if response.status_code == 401:
            raise PermissionDenied(
                "GitHub rejected the token (401). It is expired, revoked, or mistyped. "
                "Run `crew auth`."
            )
        body = response.json()
        if body.get("errors"):
            message = body["errors"][0].get("message", "unknown error")
            if "not have permission" in message or "Resource not accessible" in message:
                raise PermissionDenied(
                    f"{message}. The token likely lacks a grant — see docs/agent-auth.md."
                )
            raise BoardError(message)
        return body.get("data") or {}

    def _project(self, query: str, **variables: Any) -> dict[str, Any]:
        """Execute a *query* rooted at the board and return the projectV2 node."""
        project = ((self._call(query, **variables)).get("organization") or {}).get("projectV2")
        if project is None:
            raise BoardError(
                f"board {self.owner}/#{self.number} not visible to this token. "
                "It must be organization-owned and the token must grant "
                "Projects: Read and write."
            )
        return project

    # --- read -----------------------------------------------------------
    @cached_property
    def schema(self) -> BoardSchema:
        """Field and option ids. Cached — they change only when the board does."""
        project = self._project(_FIELDS_QUERY, owner=self.owner, number=self.number)
        fields: dict[str, BoardField] = {}
        for node in project["fields"]["nodes"]:
            if not node or "name" not in node:
                continue
            options = {o["name"]: o["id"] for o in node.get("options") or []}
            config = node.get("configuration") or {}
            for it in config.get("iterations") or []:
                options[it["title"]] = it["id"]
            fields[node["name"]] = BoardField(
                id=node["id"],
                name=node["name"],
                data_type=node.get("dataType", ""),
                options=options,
            )
        return BoardSchema(project_id=project["id"], title=project["title"], fields=fields)

    def cards(self) -> list[Card]:
        """Every item on the board, following pagination."""
        cards: list[Card] = []
        cursor: str | None = None
        while True:
            project = self._project(
                _ITEMS_QUERY, owner=self.owner, number=self.number, cursor=cursor
            )
            items = project["items"]
            for node in items["nodes"]:
                card = _to_card(node)
                if card is not None:
                    cards.append(card)
            page = items["pageInfo"]
            if not page["hasNextPage"]:
                return cards
            cursor = page["endCursor"]

    def counts(self) -> dict[str, int]:
        """Occupancy per status column — what WIP enforcement needs."""
        counts: dict[str, int] = {}
        for card in self.cards():
            if card.status:
                counts[card.status] = counts.get(card.status, 0) + 1
        return counts

    # --- write ----------------------------------------------------------
    def set_status(self, item_id: str, column: str) -> None:
        """Move a card. Legality is the caller's business — see crew_org.process."""
        self.set_select(item_id, "Status", column)

    def set_select(self, item_id: str, field: str, option: str) -> None:
        self._call(
            _SET_SELECT,
            project=self.schema.project_id,
            item=item_id,
            field=self.schema.field(field).id,
            option=self.schema.option_id(field, option),
        )

    def set_number(self, item_id: str, field: str, value: float) -> None:
        self._call(
            _SET_NUMBER,
            project=self.schema.project_id,
            item=item_id,
            field=self.schema.field(field).id,
            value=value,
        )

    def set_iteration(self, item_id: str, field: str, title: str) -> None:
        self._call(
            _SET_ITERATION,
            project=self.schema.project_id,
            item=item_id,
            field=self.schema.field(field).id,
            iteration=self.schema.option_id(field, title),
        )

    def add_issue(self, issue_node_id: str) -> str:
        """Put an existing issue on the board. Returns the new item id."""
        data = self._call(_ADD_ITEM, project=self.schema.project_id, content=issue_node_id)
        return data["addProjectV2ItemById"]["item"]["id"]

    def remove_item(self, item_id: str) -> None:
        """Take a card off the board. The issue itself is untouched."""
        self._call(_DELETE_ITEM, project=self.schema.project_id, item=item_id)


def _to_card(node: dict[str, Any]) -> Card | None:
    """Flatten one GraphQL item node. Returns None for draft items, which the
    crew does not use — every card must be a real issue with a number."""
    content = node.get("content") or {}
    if not content.get("number"):
        return None

    values: dict[str, Any] = {}
    for value in node.get("fieldValues", {}).get("nodes") or []:
        field_name = ((value or {}).get("field") or {}).get("name")
        attr = _FIELD_TO_ATTR.get(field_name)
        if attr is None:
            continue
        values[attr] = value.get("name") or value.get("title") or value.get("number")

    return Card(
        item_id=node["id"],
        number=content["number"],
        title=content.get("title", ""),
        url=content.get("url"),
        repo=(content.get("repository") or {}).get("name"),
        state=content.get("state"),
        labels=frozenset(
            label["name"] for label in (content.get("labels") or {}).get("nodes") or []
        ),
        **values,
    )
