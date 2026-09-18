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
    required = ("board", "wip_limits", "sprint", "design", "estimation", "execution", "escalation")
    for key in required:
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

    design = org["design"]
    if design["force_label"] == design["skip_label"]:
        raise ValueError("design.force_label and design.skip_label must differ")

    # A typo here would silently disable the sandbox, so it fails at startup
    # rather than the first time generated code runs.
    sandbox = org.get("sandbox") or {}
    mode = sandbox.get("mode", "required")
    if mode not in ("required", "off"):
        raise ValueError(
            f"sandbox.mode must be 'required' or 'off', got {mode!r}. "
            "An unrecognised value would be treated as neither."
        )

    overlap = set(org["escalation"]["never_escalate"]) & set(org["escalation"]["may_escalate"])
    if overlap:
        raise ValueError(
            f"escalation classes cannot be both never_escalate and may_escalate: {sorted(overlap)}"
        )


def load_env(path: Path | None = None) -> dict[str, str]:
    """Read .env into a dict. Values already exported win, as in a shell."""
    import os

    target = path or Path(".env")
    values: dict[str, str] = {}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith("GITHUB_")})
    return values
