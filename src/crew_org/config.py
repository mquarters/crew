"""Loading of organization policy.

Process constants live in config/org.yaml so that changing how the org works is
a config change reviewed like any other, not a code edit buried in a prompt.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parent / "config"
ORG_CONFIG = CONFIG_DIR / "org.yaml"


@cache
def load_org(path: Path | None = None) -> dict[str, Any]:
    """Load and validate org.yaml."""
    target = path or ORG_CONFIG
    with target.open(encoding="utf-8") as fh:
        org: dict[str, Any] = yaml.safe_load(fh)
    _validate(org)
    return org


def _validate(org: dict[str, Any]) -> None:
    """Fail loudly at startup rather than mid-sprint."""
    for key in ("board", "wip_limits", "sprint", "estimation", "execution", "escalation"):
        if key not in org:
            raise ValueError(f"org.yaml is missing required section: {key!r}")

    columns = org["board"]["columns"]
    blocked = org["board"]["blocked_column"]
    if blocked in columns:
        raise ValueError(
            f"{blocked!r} must not appear in board.columns — it is off-flow and "
            "reachable from any column."
        )

    unknown = set(org["wip_limits"]) - set(columns)
    if unknown:
        raise ValueError(f"wip_limits names columns not on the board: {sorted(unknown)}")

    gates = set(org["board"]["human_gates"]) - set(columns)
    if gates:
        raise ValueError(f"board.human_gates names columns not on the board: {sorted(gates)}")

    overlap = set(org["escalation"]["never_escalate"]) & set(org["escalation"]["may_escalate"])
    if overlap:
        raise ValueError(
            f"escalation classes cannot be both never_escalate and may_escalate: {sorted(overlap)}"
        )
