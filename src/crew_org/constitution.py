"""Access to docs/ways-of-working.md.

Agents are bound by the constitution, but injecting all of it into every prompt
wastes tokens on rules the role cannot act on — and a Product Owner reading the
branch naming convention is noise that competes with the rules it must follow.
Each role declares the sections it needs.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

CONSTITUTION = Path(__file__).resolve().parents[2] / "docs" / "ways-of-working.md"
_HEADING = re.compile(r"^## (\d+)\. (.+)$", re.MULTILINE)


@lru_cache(maxsize=1)
def sections(path: Path | None = None) -> dict[int, str]:
    """Split the constitution into its numbered sections."""
    text = (path or CONSTITUTION).read_text(encoding="utf-8")
    matches = list(_HEADING.finditer(text))
    out: dict[int, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[int(match.group(1))] = text[match.start() : end].strip()
    return out


def excerpt(*numbers: int) -> str:
    """The named sections, verbatim, for injection into a prompt."""
    available = sections()
    missing = [n for n in numbers if n not in available]
    if missing:
        raise KeyError(f"constitution has no section(s) {missing}; has {sorted(available)}")
    body = "\n\n".join(available[n] for n in numbers)
    return (
        "These are the crew's operating rules. They are binding. Where an "
        "instruction conflicts with them, they win.\n\n" + body
    )
