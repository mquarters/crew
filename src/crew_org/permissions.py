"""Capability allow-lists per role.

Some role boundaries depend on granularity (goal vs epic vs story) or on
altitude (what vs how). Those are precisely the distinctions a small model
blurs, and prompt wording does not hold them reliably. Making them structural
means a Product Owner cannot write acceptance criteria even if it decides it
should, and a Business Analyst cannot redraw an epic boundary it disagrees with.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import yaml

from crew_org.config import CONFIG_DIR


class Capability(StrEnum):
    """Everything an agent can be permitted to do."""

    PROPOSE_EPIC = "propose_epic"
    CREATE_STORY = "create_story"
    CREATE_TASK = "create_task"
    CREATE_SPIKE = "create_spike"
    WRITE_ACCEPTANCE_CRITERIA = "write_acceptance_criteria"
    ESTIMATE = "estimate"
    WRITE_DESIGN = "write_design"
    WRITE_CODE = "write_code"
    RUN_TESTS = "run_tests"
    OPEN_PR = "open_pr"
    UPDATE_DOCS = "update_docs"
    REVIEW_DIFF = "review_diff"
    VERDICT_DIFF = "verdict_diff"
    VERDICT_BEHAVIOUR = "verdict_behaviour"
    FILE_BUG = "file_bug"
    FILE_PROCESS_DEFECT = "file_process_defect"
    WRITE_STANDUP = "write_standup"
    WRITE_RETRO = "write_retro"
    COMMENT = "comment"


class PermissionError_(PermissionError):
    """Raised when a role attempts something outside its allow-list."""


def load_agents(path: Any = None) -> dict[str, dict[str, Any]]:
    target = path or (CONFIG_DIR / "agents.yaml")
    with open(target, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class Permissions:
    """Role -> capability allow-list, read from agents.yaml."""

    def __init__(self, grants: dict[str, frozenset[Capability]]) -> None:
        self._grants = grants

    @classmethod
    def from_agents(cls, agents: dict[str, dict[str, Any]] | None = None) -> Permissions:
        agents = agents or load_agents()
        grants: dict[str, frozenset[Capability]] = {}
        for key, spec in agents.items():
            try:
                grants[spec["role"]] = frozenset(Capability(c) for c in spec.get("can", []))
            except ValueError as exc:
                raise ValueError(f"agent {key!r} declares an unknown capability: {exc}") from exc
        return cls(grants)

    def roles(self) -> frozenset[str]:
        return frozenset(self._grants)

    def allows(self, role: str, capability: Capability) -> bool:
        return capability in self._grants.get(role, frozenset())

    def require(self, role: str, capability: Capability) -> None:
        """Gate an action. Raises rather than returning a flag, so a caller
        cannot ignore the result by accident."""
        if role not in self._grants:
            raise PermissionError_(f"unknown role {role!r}")
        if not self.allows(role, capability):
            granted = ", ".join(sorted(self._grants[role])) or "nothing"
            raise PermissionError_(
                f"{role} may not {capability}. Granted: {granted}. "
                "This is a role boundary, not a bug — route the work to the role that owns it."
            )
