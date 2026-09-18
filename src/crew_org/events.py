"""Event sink feeding the live view and the audit trail.

The crew's own event model is the source of truth. A bridge translates CrewAI's
internal bus onto it, so the TUI and the JSONL record never depend on CrewAI
internals — those event payloads vary across versions, and a live view that
breaks on upgrade is worse than no live view.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    TICK_STARTED = "tick.started"
    TICK_FINISHED = "tick.finished"

    CARD_CLAIMED = "card.claimed"
    CARD_MOVED = "card.moved"
    CARD_BLOCKED = "card.blocked"

    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"
    AGENT_FAILED = "agent.failed"

    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"

    LLM_CALL_STARTED = "llm.started"
    LLM_CALL_FINISHED = "llm.finished"
    LLM_CALL_FAILED = "llm.failed"

    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    TOOL_FAILED = "tool.failed"

    ESCALATION_DECIDED = "escalation.decided"
    ESCALATED = "escalation.sent"

    NOTE = "note"


class CrewEvent(BaseModel):
    """One observable thing that happened. Rendered live, persisted for replay."""

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: EventKind
    role: str | None = None
    card: int | None = None
    summary: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


Subscriber = Callable[[CrewEvent], None]


class EventSink:
    """Fan-out to live subscribers plus an append-only JSONL record.

    Thread-safe: CrewAI may emit from worker threads.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def emit(self, event: CrewEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(event.model_dump_json() + "\n")
        for fn in subscribers:
            # A broken view must never take down the run that feeds it.
            with contextlib.suppress(Exception):
                fn(event)

    def note(self, kind: EventKind, summary: str, **detail: Any) -> None:
        self.emit(CrewEvent(kind=kind, summary=summary, detail=detail))

    def replay(self) -> list[CrewEvent]:
        """Read back a persisted run — used by the standup and by tests."""
        if self.path is None or not self.path.exists():
            return []
        out: list[CrewEvent] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(CrewEvent.model_validate(json.loads(line)))
        return out


# --- CrewAI bridge -------------------------------------------------------

# CrewAI event payload attributes differ between versions, so every field is
# read defensively and the mapping is data rather than code.
_BRIDGE: dict[str, EventKind] = {
    "TaskStartedEvent": EventKind.TASK_STARTED,
    "TaskCompletedEvent": EventKind.TASK_COMPLETED,
    "TaskFailedEvent": EventKind.TASK_FAILED,
    "LLMCallStartedEvent": EventKind.LLM_CALL_STARTED,
    "LLMCallCompletedEvent": EventKind.LLM_CALL_FINISHED,
    "LLMCallFailedEvent": EventKind.LLM_CALL_FAILED,
    "ToolUsageStartedEvent": EventKind.TOOL_STARTED,
    "ToolUsageFinishedEvent": EventKind.TOOL_FINISHED,
    "ToolUsageErrorEvent": EventKind.TOOL_FAILED,
    "LiteAgentExecutionStartedEvent": EventKind.AGENT_STARTED,
    "LiteAgentExecutionCompletedEvent": EventKind.AGENT_FINISHED,
    "LiteAgentExecutionErrorEvent": EventKind.AGENT_FAILED,
}


def _first_attr(obj: Any, *names: str) -> Any:
    for n in names:
        value = getattr(obj, n, None)
        if value is not None:
            return value
    return None


def bridge_crewai(sink: EventSink, *, card: int | None = None) -> None:
    """Forward CrewAI's internal bus onto the crew's sink.

    Best-effort by design: if CrewAI changes its bus, the live view degrades to
    the crew's own events rather than crashing the tick.
    """
    try:
        from crewai.events import crewai_event_bus  # noqa: PLC0415
        from crewai.events.base_events import BaseEvent  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        sink.note(EventKind.NOTE, "CrewAI event bus unavailable; live view uses crew events only.")
        return

    @crewai_event_bus.on(BaseEvent)
    def _forward(_source: Any, event: Any) -> None:  # pragma: no cover - needs a live crew
        kind = _BRIDGE.get(type(event).__name__)
        if kind is None:
            return
        role = _first_attr(event, "role", "agent_role", "from_agent")
        name = _first_attr(event, "task_name", "tool_name", "model", "description") or ""
        sink.emit(
            CrewEvent(
                kind=kind,
                role=str(role) if role else None,
                card=card,
                summary=str(name)[:120],
            )
        )
