"""The crew command line.

A tick runs to quiescence: it drains every actionable card until the board is
stable or reaches a human gate. The Sponsor controls when the process runs, not
the individual transitions within it.
"""

from __future__ import annotations

import time
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from crew_org.config import load_org
from crew_org.events import CrewEvent, EventKind, EventSink
from crew_org.tui import LiveView, attach

app = typer.Typer(help="An agile engineering organization run as agents.", no_args_is_help=True)
sprint_app = typer.Typer(help="Sprint cadence commands.", no_args_is_help=True)
app.add_typer(sprint_app, name="sprint")

console = Console()
VAR = Path("var")


def _sink(dry_run: bool) -> EventSink:
    return EventSink(None if dry_run else VAR / "events" / "tick.jsonl")


@app.command()
def tick(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Render the live view from synthetic events. Needs no model."
    ),
) -> None:
    """Drain every actionable card until the board is stable or hits a gate."""
    org = load_org()
    sink = _sink(dry_run)
    view = LiveView(org["board"]["columns"], budget=org["sprint"]["escalation_budget"])

    if not dry_run:
        console.print(
            "[yellow]Not yet wired to a board.[/] The inference substrate and GitHub "
            "bootstrap must be in place first. Run [bold]crew tick --dry-run[/] to "
            "exercise the live view, or [bold]crew doctor[/] to check the backend."
        )
        raise typer.Exit(code=1)

    with attach(sink, view):
        _synthetic_tick(sink)


def _synthetic_tick(sink: EventSink) -> None:
    """A representative tick, used to exercise the view without inference.

    Deliberately includes a VERIFY failure that repairs locally and a CAPABILITY
    failure that escalates, so the escalation panel is exercised too.
    """

    def emit(kind: EventKind, summary: str, role: str | None = None, card: int | None = None, **d):
        sink.emit(CrewEvent(kind=kind, summary=summary, role=role, card=card, detail=d))
        time.sleep(0.28)

    emit(EventKind.TICK_STARTED, "draining board", tick=1, sprint="S1")

    emit(EventKind.CARD_CLAIMED, "Goal: sprint metrics CLI", "Product Owner", 1)
    emit(EventKind.AGENT_STARTED, "decompose goal into epics", "Product Owner", 1)
    emit(EventKind.LLM_CALL_STARTED, "crew-local", "Product Owner", 1)
    emit(EventKind.LLM_CALL_FINISHED, "crew-local  1,284 tok", "Product Owner", 1)
    emit(EventKind.AGENT_FINISHED, "proposed 4 epics", "Product Owner", 1)
    emit(
        EventKind.CARD_MOVED,
        "to Needs Refinement",
        "Product Owner",
        1,
        **{"from": "Inbox (Goals)", "to": "Needs Refinement"},
    )

    emit(EventKind.AGENT_STARTED, "split epic into stories", "Business Analyst", 2)
    emit(EventKind.AGENT_FINISHED, "3 stories, AC written", "Business Analyst", 2)
    emit(
        EventKind.CARD_MOVED,
        "to Ready",
        "Business Analyst",
        2,
        **{"from": "Needs Refinement", "to": "Ready"},
    )

    emit(EventKind.CARD_CLAIMED, "Story: cycle-time metric", "Developer", 7)
    emit(
        EventKind.CARD_MOVED,
        "to In Progress",
        "Developer",
        7,
        **{"from": "Sprint Backlog", "to": "In Progress"},
    )
    emit(EventKind.AGENT_STARTED, "implement cycle-time metric", "Developer", 7)
    emit(EventKind.TOOL_STARTED, "pytest", "Developer", 7)
    emit(EventKind.TOOL_FAILED, "2 failed", "Developer", 7)
    emit(EventKind.ESCALATION_DECIDED, "VERIFY attempt 1/2 — repairing locally", "Developer", 7)
    emit(EventKind.TOOL_STARTED, "pytest", "Developer", 7)
    emit(EventKind.TOOL_FINISHED, "18 passed", "Developer", 7)
    emit(EventKind.AGENT_FINISHED, "PR #31 opened", "Developer", 7)
    emit(
        EventKind.CARD_MOVED,
        "to In Review",
        "Developer",
        7,
        **{"from": "In Progress", "to": "In Review"},
    )

    emit(EventKind.AGENT_STARTED, "design pagination strategy", "Architect", 9)
    emit(EventKind.ESCALATION_DECIDED, "CAPABILITY — justified, 1/3 of budget", "Architect", 9)
    emit(
        EventKind.ESCALATED,
        "cross-cutting GraphQL pagination",
        "Architect",
        9,
        failure_class="CAPABILITY",
    )
    emit(EventKind.AGENT_FINISHED, "design note attached", "Architect", 9)

    emit(EventKind.CARD_BLOCKED, "awaiting Sponsor epic approval", "Scrum Master", 3)
    emit(EventKind.TICK_FINISHED, "board stable — 2 cards at human gate")
    time.sleep(0.8)


@app.command()
def doctor(
    base_url: str = typer.Option(
        None, "--base-url", help="OpenAI-compatible endpoint, e.g. http://host:30000/v1"
    ),
    host: str = typer.Option(
        None, "--host", help="Probe common ports on this host to find the endpoint."
    ),
    model: str = typer.Option(None, "--model", help="Override the served model name."),
    deep: bool = typer.Option(
        False, "--deep", help="Also run a real CrewAI crew end to end. Costs tokens."
    ),
) -> None:
    """Validate the inference substrate before anything is built on it.

    Proves the two capabilities the architecture depends on: tool calling and
    constrained JSON decoding. Both are launch-flag dependent under SGLang.
    """
    from crew_org.substrate import CANDIDATE_PORTS, Status, discover, run_all

    if not base_url and not host:
        console.print("Pass [bold]--base-url[/] or [bold]--host[/] to probe.")
        raise typer.Exit(code=2)

    if not base_url:
        ports = ", ".join(str(p) for p in CANDIDATE_PORTS)
        console.print(f"[dim]Probing {host} on ports {ports}…[/]")
        base_url = discover(host)
        if not base_url:
            console.print(
                f"[red]No OpenAI-compatible endpoint found on {host}.[/] "
                "Is SGLang running, and is the host reachable?"
            )
            raise typer.Exit(code=1)
        console.print(f"[green]Found[/] {base_url}")

    results = run_all(base_url, model, deep=deep)

    table = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    table.add_column("check")
    table.add_column("")
    table.add_column("detail", ratio=1)
    marks = {
        Status.PASS: ("[green]pass[/]", ""),
        Status.FAIL: ("[red]FAIL[/]", "red"),
        Status.WARN: ("[yellow]warn[/]", "yellow"),
        Status.SKIP: ("[dim]skip[/]", "dim"),
    }
    for r in results:
        mark, style = marks[r.status]
        table.add_row(r.check, mark, Text(r.detail, style=style or "") if style else r.detail)
    console.print(table)

    for r in results:
        if r.hint:
            console.print(f"[yellow]→[/] [bold]{r.check}:[/] {r.hint}")

    failed = [r for r in results if r.status is Status.FAIL]
    if failed:
        console.print(
            "\n[red]Phase 0 gate not passed.[/] Build nothing on the substrate until "
            "these clear — a failure here shows up later as unexplained agent failures."
        )
        raise typer.Exit(code=1)
    if any(r.status is Status.WARN for r in results):
        console.print("\n[yellow]Phase 0 gate passed with warnings.[/]")
        return
    console.print("\n[green]Phase 0 gate passed.[/] Substrate is sound.")


@app.command()
def auth() -> None:
    """Verify the credential the agents use — including what it must NOT do."""
    from crew_org.auth import Status as AuthStatus
    from crew_org.auth import load_token, verify
    from crew_org.config import load_env

    env = load_env()
    token = load_token()
    if not token:
        console.print(
            "[red]No GITHUB_TOKEN.[/] Create a fine-grained token and add it to .env — "
            "see [bold]docs/agent-auth.md[/] for the exact permissions."
        )
        raise typer.Exit(code=2)

    owner = env.get("GITHUB_OWNER")
    if not owner:
        console.print("[red]GITHUB_OWNER is not set in .env.[/] Run scripts/bootstrap_github.sh.")
        raise typer.Exit(code=2)

    repos = [r for r in (env.get("CREW_REPO"), env.get("PILOT_REPO")) if r]
    checks = verify(
        token,
        owner=owner,
        repos=repos,
        project_number=int(env.get("GITHUB_PROJECT_NUMBER", 0)),
        owner_is_org=env.get("GITHUB_OWNER_TYPE", "organization") == "organization",
    )

    table = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    table.add_column("check")
    table.add_column("")
    table.add_column("detail", ratio=1)
    marks = {
        AuthStatus.PASS: "[green]pass[/]",
        AuthStatus.FAIL: "[red]FAIL[/]",
        AuthStatus.WARN: "[yellow]warn[/]",
        AuthStatus.SKIP: "[dim]skip[/]",
    }
    for c in checks:
        table.add_row(c.check, marks[c.status], c.detail)
    console.print(table)

    for c in checks:
        if c.hint:
            console.print(f"[yellow]→[/] [bold]{c.check}:[/] {c.hint}")

    if any(c.status is AuthStatus.FAIL for c in checks):
        console.print("\n[red]Token is not fit for the crew.[/]")
        raise typer.Exit(code=1)
    if any(c.status is AuthStatus.WARN for c in checks):
        console.print("\n[yellow]Token usable, with warnings.[/]")
        return
    console.print("\n[green]Token is correctly scoped.[/]")


@sprint_app.command("start")
def sprint_start() -> None:
    """Open a sprint and admit cards from Ready."""
    console.print("[yellow]Not yet implemented.[/] Requires the board (Phase 2).")
    raise typer.Exit(code=1)


@sprint_app.command("close")
def sprint_close() -> None:
    """Close a sprint: review summary, standup, and retro from the ledger."""
    console.print("[yellow]Not yet implemented.[/] Requires the board (Phase 2).")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
