"""Live view of what the crew is doing during a tick.

Driven entirely by the crew's own EventSink, so it renders identically whether
events come from a real run, a CrewAI bridge, or a replayed JSONL file.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from crew_org.events import CrewEvent, EventKind, EventSink

_ACTIVITY_START = {EventKind.AGENT_STARTED, EventKind.TASK_STARTED}
_ACTIVITY_END = {
    EventKind.AGENT_FINISHED,
    EventKind.AGENT_FAILED,
    EventKind.TASK_COMPLETED,
    EventKind.TASK_FAILED,
}

_KIND_STYLE = {
    EventKind.TASK_FAILED: "red",
    EventKind.AGENT_FAILED: "red",
    EventKind.TOOL_FAILED: "red",
    EventKind.LLM_CALL_FAILED: "red",
    EventKind.CARD_BLOCKED: "yellow",
    EventKind.ESCALATED: "magenta",
    EventKind.ESCALATION_DECIDED: "magenta",
    EventKind.CARD_MOVED: "green",
    EventKind.TASK_COMPLETED: "green",
}


@dataclass
class ActiveAgent:
    role: str
    card: int | None
    summary: str
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class LiveView:
    """Mutable render state, updated one event at a time."""

    def __init__(self, columns: list[str], *, budget: int, tail: int = 12) -> None:
        self.columns = columns
        self.budget = budget
        self.tick = 0
        self.sprint = "—"
        self.started = time.monotonic()
        self.board: dict[str, int] = dict.fromkeys(columns, 0)
        self.blocked = 0
        self.escalations: list[str] = []
        self.active: dict[str, ActiveAgent] = {}
        self.events: deque[CrewEvent] = deque(maxlen=tail)

    # --- state ---------------------------------------------------------
    def handle(self, event: CrewEvent) -> None:
        self.events.append(event)

        if event.kind is EventKind.TICK_STARTED:
            self.tick = int(event.detail.get("tick", self.tick + 1))
            self.sprint = str(event.detail.get("sprint", self.sprint))
            self.started = time.monotonic()

        elif event.kind is EventKind.CARD_MOVED:
            for key in ("from", "to"):
                column = event.detail.get(key)
                if column in self.board:
                    self.board[column] += 1 if key == "to" else -1
                    self.board[column] = max(self.board[column], 0)

        elif event.kind is EventKind.CARD_BLOCKED:
            self.blocked += 1

        elif event.kind is EventKind.ESCALATED:
            self.escalations.append(
                f"#{event.card} {event.detail.get('failure_class', '?')} — {event.summary}"
            )

        if event.kind in _ACTIVITY_START and event.role:
            self.active[event.role] = ActiveAgent(event.role, event.card, event.summary)
        elif event.kind in _ACTIVITY_END and event.role:
            self.active.pop(event.role, None)

    def set_board(self, counts: dict[str, int], blocked: int = 0) -> None:
        """Seed swimlane counts from the real board at the start of a tick."""
        for column, n in counts.items():
            if column in self.board:
                self.board[column] = n
        self.blocked = blocked

    # --- render --------------------------------------------------------
    def render(self) -> RenderableType:
        return Group(
            self._header(),
            self._swimlanes(),
            self._agents(),
            self._escalations(),
            self._log(),
        )

    def _header(self) -> RenderableType:
        elapsed = time.monotonic() - self.started
        text = Text.assemble(
            ("sprint ", "dim"),
            (f"{self.sprint}", "bold"),
            ("   tick ", "dim"),
            (f"{self.tick}", "bold"),
            ("   elapsed ", "dim"),
            (f"{elapsed:5.1f}s", "bold"),
            ("   active ", "dim"),
            (f"{len(self.active)}", "bold cyan"),
        )
        return Panel(text, title="crew", border_style="cyan", padding=(0, 1))

    def _swimlanes(self) -> RenderableType:
        table = Table.grid(padding=(0, 2))
        for _ in range(len(self.columns) + 1):
            table.add_column(justify="center")
        table.add_row(*[Text(c, style="dim") for c in self.columns], Text("Blocked", style="dim"))
        table.add_row(
            *[Text(str(self.board[c]), style="bold") for c in self.columns],
            Text(str(self.blocked), style="bold yellow" if self.blocked else "bold"),
        )
        return Panel(table, title="board", border_style="blue", padding=(0, 1))

    def _agents(self) -> RenderableType:
        if not self.active:
            return Panel(Text("idle", style="dim"), title="agents", border_style="grey50")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column(justify="right", style="cyan")
        table.add_column(ratio=1)
        table.add_column(justify="right", style="dim")
        for agent in self.active.values():
            table.add_row(
                agent.role,
                f"#{agent.card}" if agent.card else "—",
                Text(agent.summary[:70], overflow="ellipsis"),
                f"{agent.elapsed:4.1f}s",
            )
        return Panel(table, title="agents", border_style="green", padding=(0, 1))

    def _escalations(self) -> RenderableType:
        spent = len(self.escalations)
        style = "red" if spent >= self.budget else "magenta"
        body: RenderableType
        if not self.escalations:
            body = Text("none", style="dim")
        else:
            body = Text("\n".join(self.escalations[-4:]))
        return Panel(
            body,
            title=f"escalation  {spent}/{self.budget}",
            border_style=style,
            padding=(0, 1),
        )

    def _log(self) -> RenderableType:
        lines = Text()
        for event in self.events:
            style = _KIND_STYLE.get(event.kind, "white")
            lines.append(f"{event.at:%H:%M:%S} ", style="dim")
            lines.append(f"{event.kind:<20} ", style=style)
            if event.role:
                lines.append(f"{event.role} ", style="cyan")
            lines.append(f"{event.summary}\n")
        return Panel(lines or Text("—", style="dim"), title="log", border_style="grey50")


def attach(sink: EventSink, view: LiveView, *, console: Console | None = None) -> Live:
    """Subscribe `view` to `sink` and return a started-on-enter Live context."""
    console = console or Console()
    live = Live(view.render(), console=console, refresh_per_second=8, transient=False)

    def on_event(event: CrewEvent) -> None:
        view.handle(event)
        live.update(view.render())

    sink.subscribe(on_event)
    return live
