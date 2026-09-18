"""When an epic gets a design note.

The Architect is the role a local 27B model performs worst at, and the one most
likely to exhaust the sprint's escalation budget. Rather than let every epic
pull design work through that bottleneck, design is rationed: only epics above a
complexity threshold get a design note.

The tradeoff is explicit. Skipping design on a small epic risks a developer
inventing an approach; requiring it on every epic guarantees budget pressure.
The thresholds in config/org.yaml are the dial between those.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EpicShape(BaseModel):
    """What we know about an epic's size at refinement time."""

    points_total: int = Field(ge=0)
    story_count: int = Field(ge=0)
    modules_touched: int = Field(ge=0, default=0)
    labels: frozenset[str] = frozenset()


class DesignDecision(BaseModel):
    required: bool
    reason: str


class DesignPolicy:
    """Decides whether an epic warrants a design note."""

    def __init__(
        self,
        *,
        min_points: int,
        min_stories: int,
        min_modules: int,
        force_label: str,
        skip_label: str,
    ) -> None:
        self.min_points = min_points
        self.min_stories = min_stories
        self.min_modules = min_modules
        self.force_label = force_label
        self.skip_label = skip_label

    @classmethod
    def from_config(cls, org: dict[str, Any]) -> DesignPolicy:
        d = org["design"]
        return cls(
            min_points=d["min_points"],
            min_stories=d["min_stories"],
            min_modules=d["min_modules"],
            force_label=d["force_label"],
            skip_label=d["skip_label"],
        )

    def decide(self, epic: EpicShape) -> DesignDecision:
        # An explicit human instruction outranks every threshold, in both
        # directions. force wins over skip: demanding design is the safer error.
        if self.force_label in epic.labels:
            return DesignDecision(
                required=True, reason=f"{self.force_label!r} was applied explicitly."
            )
        if self.skip_label in epic.labels:
            return DesignDecision(
                required=False, reason=f"{self.skip_label!r} waives design for this epic."
            )

        triggers = []
        if epic.points_total >= self.min_points:
            triggers.append(f"{epic.points_total} points (>= {self.min_points})")
        if epic.story_count >= self.min_stories:
            triggers.append(f"{epic.story_count} stories (>= {self.min_stories})")
        if epic.modules_touched >= self.min_modules:
            triggers.append(f"{epic.modules_touched} modules (>= {self.min_modules})")

        if triggers:
            return DesignDecision(required=True, reason="Above threshold: " + "; ".join(triggers))
        return DesignDecision(
            required=False,
            reason=(
                f"Below every threshold ({epic.points_total} points, {epic.story_count} "
                f"stories, {epic.modules_touched} modules); stories go straight to Ready."
            ),
        )
