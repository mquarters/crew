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
from rich.markup import escape
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

    if dry_run:
        with attach(sink, view):
            _synthetic_tick(sink)
        return

    # The proxy is project-scoped and will not always be running. Say so plainly
    # rather than surfacing a connection error from deep inside an agent.
    from crew_org.auth import resolve_credentials
    from crew_org.config import load_env
    from crew_org.flows.board_flow import tick as run_tick
    from crew_org.llm import health
    from crew_org.tools.github_issues import IssueClient
    from crew_org.tools.github_project import ProjectClient

    ok, message = health()
    if not ok:
        console.print(f"[red]{message}[/]")
        raise typer.Exit(code=1)
    console.print(f"[dim]{message}[/]")

    env = load_env()
    try:
        token, identity = resolve_credentials(env)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    console.print(f"[dim]acting as {escape(identity)}[/]")

    owner = env["GITHUB_OWNER"]
    board = ProjectClient(token, owner, int(env["GITHUB_PROJECT_NUMBER"]))
    issues = IssueClient(token, owner)

    with attach(sink, view):
        result = run_tick(board, issues, sink, default_repo=env.get("PILOT_REPO", "crew"))

    console.print()
    if result.considered == 0:
        console.print(
            "[dim]Nothing in Inbox (Goals). File a goal issue and add it to the board.[/]"
        )
    for number in result.proposed:
        console.print(f"[green]proposed[/] epics on #{number}")
    for number in result.epics_refined:
        console.print(f"[green]refined[/]  #{number} into stories")
    if result.design_required:
        console.print(
            f"[yellow]design required[/] on {', '.join(f'#{n}' for n in result.design_required)}"
        )
    for number, why in result.skipped:
        console.print(f"[dim]skipped[/]  #{number} — {why}")
    for number, why in result.failed:
        console.print(f"[red]failed[/]   #{number} — {why}")
    if result.stories_created:
        console.print(
            f"\n[bold]{len(result.stories_created)} stories are in Ready.[/] "
            "Review them, or start a sprint when the backlog looks right."
        )
    elif result.proposed:
        console.print(
            "\n[bold]Read the proposals on the cards.[/] Nothing was moved — approve by "
            "moving a card out of Inbox (Goals), or comment with changes."
        )


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
        table.add_row(r.check, mark, Text(r.detail, style=style or ""))
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
    from crew_org.auth import app_permissions, resolve_credentials, verify
    from crew_org.config import load_env

    env = load_env()
    try:
        token, identity = resolve_credentials(env)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    console.print(f"[dim]identity: {escape(identity)}[/]")

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
        app_slug=identity if identity != "personal access token" else None,
        app_permissions=app_permissions(),
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
        table.add_row(c.check, marks[c.status], escape(c.detail))
    console.print(table)

    for c in checks:
        if c.hint:
            console.print(f"[yellow]→[/] [bold]{c.check}:[/] {escape(c.hint)}")

    if any(c.status is AuthStatus.FAIL for c in checks):
        console.print("\n[red]Token is not fit for the crew.[/]")
        raise typer.Exit(code=1)
    if any(c.status is AuthStatus.WARN for c in checks):
        console.print("\n[yellow]Token usable, with warnings.[/]")
        return
    console.print("\n[green]Token is correctly scoped.[/]")


@app.command()
def qa() -> None:
    """Verify delivered work against its acceptance criteria."""
    from crew_org.auth import resolve_credentials
    from crew_org.config import load_env, load_org
    from crew_org.flows.acceptance import close_finished_parents, run_qa
    from crew_org.git_ops import Workspace
    from crew_org.llm import health
    from crew_org.tools.github_issues import IssueClient
    from crew_org.tools.github_project import ProjectClient
    from crew_org.tools.sandbox import Sandbox

    org, env = load_org(), load_env()
    ok, message = health()
    if not ok:
        console.print(f"[red]{message}[/]")
        raise typer.Exit(code=1)

    box = Sandbox.from_config(org)
    blocked = box.unavailable_reason()
    if blocked:
        console.print(f"[red]Sandbox unavailable.[/] {blocked}")
        raise typer.Exit(code=1)

    token, identity = resolve_credentials(env)
    owner, repo = env["GITHUB_OWNER"], env.get("PILOT_REPO", "crew")
    board = ProjectClient(token, owner, int(env["GITHUB_PROJECT_NUMBER"]))
    issues = IssueClient(token, owner)
    ws = Workspace(owner, repo, token, _bot_identity(token, identity))

    sink = EventSink(VAR / "events" / "qa.jsonl")
    cards = board.cards()
    result = run_qa(board, issues, sink, ws, box, cards=cards, repo=repo)
    result.parents_closed = close_finished_parents(board, issues, sink, board.cards(), repo=repo)

    console.print()
    for outcome in result.verified:
        console.print(f"[green]#{outcome.card}[/] accepted")
    for outcome in result.returned:
        console.print(f"[yellow]#{outcome.card}[/] returned — {outcome.unproven} criteria unproven")
    for number, why in result.failed:
        console.print(f"[red]#{number}[/] {why}")
    for number in result.parents_closed:
        console.print(f"[green]#{number}[/] closed — all children done")
    if not (result.verified or result.returned or result.failed):
        console.print("[dim]Nothing In Review.[/]")


@app.command()
def review(
    repo: str = typer.Option(None, "--repo", help="Defaults to the pilot repo."),
) -> None:
    """Review every open pull request that has no crew verdict yet.

    Human-authored pull requests are reviewed on the same terms as the crew's.
    """
    from crew_org.auth import resolve_credentials
    from crew_org.config import load_env
    from crew_org.flows.review import review_open_pulls
    from crew_org.llm import health
    from crew_org.tools.github_issues import IssueClient

    ok, message = health()
    if not ok:
        console.print(f"[red]{message}[/]")
        raise typer.Exit(code=1)

    env = load_env()
    token, identity = resolve_credentials(env)
    owner = env["GITHUB_OWNER"]
    repo = repo or env.get("PILOT_REPO", "crew")

    console.print(f"[dim]acting as {escape(identity)} · reviewing {owner}/{repo}[/]")
    sink = EventSink(VAR / "events" / "review.jsonl")
    result = review_open_pulls(IssueClient(token, owner), sink, repo=repo, bot_login=identity)

    console.print()
    for outcome in result.reviewed:
        mark = "[green]approved[/]" if outcome.approved else "[yellow]changes requested[/]"
        console.print(f"PR #{outcome.pr} — {mark}, {outcome.findings} findings")
    for outcome in result.skipped:
        console.print(f"[dim]PR #{outcome.pr} — skipped ({outcome.skipped})[/]")
    for number, why in result.failed:
        console.print(f"[red]PR #{number}[/] — {why}")
    if not (result.reviewed or result.skipped or result.failed):
        console.print("[dim]No open pull requests.[/]")


@app.command()
def deliver(
    land: bool = typer.Option(
        False, "--land", help="Actually commit, push and open PRs. Off by default."
    ),
    limit: int = typer.Option(1, "--limit", help="How many stories to attempt."),
    sprint: str = typer.Option(None, "--sprint", help="Iteration name."),
) -> None:
    """Take sprint stories to a pull request.

    Dry by default: the work is implemented and verified in the sandbox, and the
    diff is shown rather than landed. Pass --land when you want it to push.
    """
    from crew_org.auth import resolve_credentials
    from crew_org.config import load_env
    from crew_org.escalation import EscalationLedger, EscalationPolicy
    from crew_org.flows.delivery import deliver as run_delivery
    from crew_org.git_ops import Workspace
    from crew_org.llm import health
    from crew_org.process import ProcessRules
    from crew_org.tools.github_issues import IssueClient
    from crew_org.tools.github_project import ProjectClient
    from crew_org.tools.sandbox import Sandbox

    org, env = load_org(), load_env()

    ok, message = health()
    if not ok:
        console.print(f"[red]{message}[/]")
        raise typer.Exit(code=1)

    box = Sandbox.from_config(org)
    blocked = box.unavailable_reason()
    if blocked:
        console.print(f"[red]Sandbox unavailable.[/] {blocked}")
        raise typer.Exit(code=1)

    token, identity = resolve_credentials(env)
    owner = env["GITHUB_OWNER"]
    repo = env.get("PILOT_REPO", "crew")
    board = ProjectClient(token, owner, int(env["GITHUB_PROJECT_NUMBER"]))
    issues = IssueClient(token, owner)
    sprint = sprint or board.schema.field("Sprint").current_iteration()

    bot = _bot_identity(token, identity)
    ws = Workspace(owner, repo, token, bot)

    console.print(
        f"[dim]acting as {escape(identity)} · sandbox {box.mode} · "
        f"{'LANDING' if land else 'dry run'} · {sprint}[/]"
    )

    sink = EventSink(VAR / "events" / "deliver.jsonl")
    view = LiveView(org["board"]["columns"], budget=org["sprint"]["escalation_budget"])
    with attach(sink, view):
        result = run_delivery(
            board,
            issues,
            sink,
            ProcessRules.from_config(org),
            EscalationPolicy.from_config(org),
            EscalationLedger(VAR / "ledger" / "escalations.jsonl"),
            ws,
            sprint=sprint,
            repo=repo,
            dry_run=not land,
            limit=limit,
        )

    console.print()
    for outcome in result.delivered:
        if outcome.landed:
            console.print(f"[green]#{outcome.card}[/] → PR #{outcome.pr} on `{outcome.branch}`")
        else:
            path = VAR / "diffs" / f"{outcome.card}.diff"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(outcome.diff or "")
            lines = len((outcome.diff or "").splitlines())
            console.print(
                f"[green]#{outcome.card}[/] verified — {lines} diff lines, nothing landed\n"
                f"   [dim]{path}[/]"
            )
    for outcome in result.blocked:
        console.print(f"[red]#{outcome.card}[/] {outcome.blocked_reason}")
    if result.not_ours:
        console.print(
            "[dim]not the crew's to build: "
            + ", ".join(f"#{n}" for n in result.not_ours)
            + " — tracked on the board, outside delivery.repos[/]"
        )
    if result.rate_limited:
        console.print(
            "\n[yellow]Stopped on a subscription usage limit.[/] Resume with another run."
        )
    if not result.delivered and not result.blocked:
        console.print("[dim]Nothing in Sprint Backlog for this sprint.[/]")


def _bot_identity(token: str, identity: str):
    """Resolve the bot's numeric id, which GitHub needs for commit attribution."""
    import httpx

    from crew_org.git_ops import BotIdentity

    login = identity
    response = httpx.get(
        f"https://api.github.com/users/{login}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    return BotIdentity(login=login, user_id=response.json()["id"])


@sprint_app.command("start")
def sprint_start(
    sprint: str = typer.Option(
        None, "--sprint", help="Iteration name. Defaults to the current one."
    ),
    capacity: int = typer.Option(None, "--capacity", help="Points. Defaults to org.yaml."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan without admitting it."),
) -> None:
    """Fill the sprint from approved epics.

    Approving an epic was the scope decision, so this is mechanical: stories are
    pulled in priority order until capacity is reached.
    """
    from crew_org.auth import resolve_credentials
    from crew_org.config import load_env, load_org
    from crew_org.events import EventSink
    from crew_org.flows.sprint import plan_sprint, start_sprint
    from crew_org.process import ProcessRules
    from crew_org.tools.github_issues import IssueClient
    from crew_org.tools.github_project import ProjectClient

    org, env = load_org(), load_env()
    token, identity = resolve_credentials(env)
    owner = env["GITHUB_OWNER"]
    board = ProjectClient(token, owner, int(env["GITHUB_PROJECT_NUMBER"]))
    issues = IssueClient(token, owner)

    sprint = sprint or board.schema.field("Sprint").current_iteration()
    if not sprint:
        console.print("[red]No iterations configured on the Sprint field.[/]")
        raise typer.Exit(code=1)
    capacity = capacity or org["sprint"]["capacity_points"]

    sink = EventSink(None)
    rules = ProcessRules.from_config(org)
    repo = env.get("PILOT_REPO", "crew")

    if dry_run:
        cards = board.cards()
        parents = {}
        from crew_org.flows.sprint import approved_epics

        for epic in approved_epics(cards):
            for child in issues.sub_issues(epic.repo or repo, epic.number or 0):
                parents[child["number"]] = epic.number
        plan = plan_sprint(cards, parents, sprint=sprint, capacity=capacity)
    else:
        plan = start_sprint(
            board,
            issues,
            sink,
            rules,
            sprint=sprint,
            capacity=capacity,
            default_repo=repo,
        )

    _render_plan(plan, dry_run=dry_run)


def _render_plan(plan, *, dry_run: bool) -> None:
    """Report at the epic level. A Sponsor reading story-by-story is back in the work."""
    verb = "would admit" if dry_run else "admitted"
    console.print()
    table = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    table.add_column("epic")
    table.add_column("stories", justify="right")
    table.add_column("points", justify="right")
    table.add_column("")
    for piece in plan.slices:
        table.add_row(
            f"#{piece.number} {piece.title[:46]}",
            str(len(piece.admitted))
            + (f" of {len(piece.admitted) + len(piece.deferred)}" if piece.deferred else ""),
            str(piece.points),
            "[green]complete[/]" if piece.complete else "[yellow]partial[/]",
        )
    console.print(table)
    console.print(
        f"[bold]{plan.sprint}[/] — {verb} {len(plan.admitted)} stories, "
        f"{plan.points} of {plan.capacity} points"
    )
    partial = [p for p in plan.slices if not p.complete]
    if partial:
        console.print(
            "[dim]Deferred to the next sprint: "
            + ", ".join(f"#{p.number} ({len(p.deferred)} stories)" for p in partial)
            + "[/]"
        )
    if plan.unparented:
        console.print(
            f"[yellow]Not admitted[/] — {len(plan.unparented)} stories have no parent epic: "
            + ", ".join(f"#{n}" for n in plan.unparented)
        )


@sprint_app.command("close")
def sprint_close(
    sprint: str = typer.Option(None, "--sprint", help="Iteration name."),
    no_merge: bool = typer.Option(False, "--no-merge", help="Report without merging."),
) -> None:
    """Close the sprint: merge what you approved, and report on the increment.

    This is the second gate. The crew cannot approve its own pull requests, so
    reviewing the increment is approving the pull requests that make it up.
    """
    from crew_org.auth import resolve_credentials
    from crew_org.config import load_env
    from crew_org.escalation import EscalationLedger
    from crew_org.flows.close import close_sprint
    from crew_org.llm import health
    from crew_org.tools.github_issues import IssueClient
    from crew_org.tools.github_project import ProjectClient

    env = load_env()
    ok, message = health()
    if not ok:
        console.print(f"[red]{message}[/]")
        raise typer.Exit(code=1)

    token, _ = resolve_credentials(env)
    owner, repo = env["GITHUB_OWNER"], env.get("PILOT_REPO", "crew")
    board = ProjectClient(token, owner, int(env["GITHUB_PROJECT_NUMBER"]))
    sprint = sprint or board.schema.field("Sprint").current_iteration()

    result = close_sprint(
        board,
        IssueClient(token, owner),
        EventSink(VAR / "events" / "close.jsonl"),
        EscalationLedger(VAR / "ledger" / "escalations.jsonl"),
        sprint=sprint,
        repo=repo,
        merge=not no_merge,
    )

    console.print(f"\n[bold]{result.sprint}[/]")
    for number in result.merged:
        console.print(f"  [green]#{number}[/] merged and done")
    for story, pull in result.awaiting_approval:
        console.print(
            f"  [yellow]#{story}[/] waiting on your approval of PR #{pull} — "
            f"https://github.com/{owner}/{repo}/pull/{pull}"
        )
    for number, why in result.unmergeable:
        console.print(f"  [red]#{number}[/] {why}")
    if result.still_open:
        console.print(
            f"  [dim]{len(result.still_open)} stories did not reach QA: "
            + ", ".join(f"#{n}" for n in result.still_open)
            + "[/]"
        )

    if result.retro:
        console.print(f"\n[bold]Retro[/]\n{result.retro.summary}")
        for defect in result.retro.defects:
            console.print(f"\n[yellow]{defect.subject}[/] — {defect.problem}")
            console.print(f"  → {defect.change}")

    if result.complete:
        console.print("\n[green]Sprint complete.[/]")


if __name__ == "__main__":
    app()
