"""Self-contained CLI utility command handlers."""

import argparse
from pathlib import Path

from rich.console import Console

from ..ports.repository_host import DependencyIssueSnapshot

console = Console()


class _DemoIssueChecker:
    """Mock checker simulating GitHub issue states."""

    def __init__(self):
        # Issue #1 is closed (satisfied), others are open
        self.states = {1: "closed", 2: "open", 3: "open", 4: "open", 5: "open"}
        # All mock issues are in M1 for demo purposes
        self.milestones = {1: "M1", 2: "M1", 3: "M1", 4: "M1", 5: "M1"}

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        state = self.states.get(issue_number)
        if state is None:
            return None
        return DependencyIssueSnapshot(
            state=state,
            milestone=self.milestones.get(issue_number),
        )

    def get_issue_state(
        self, issue_number: int, repo: str | None = None
    ) -> str | None:
        return self.states.get(issue_number)

    def get_issue_milestone(
        self, issue_number: int, repo: str | None = None
    ) -> str | None:
        return self.milestones.get(issue_number)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run unified diagnostics on configuration and environment."""
    from rich.console import Console

    from ..execution.command_runner import LocalCommandRunner
    from ..infra.doctor import run_doctor

    console = Console()
    console.print("[bold]Issue Orchestrator Doctor[/bold]\n")

    # Get config path from args
    config_path = None
    if hasattr(args, "config") and args.config:
        config_path = Path(args.config)

    # Run diagnostics
    result = run_doctor(config_path=config_path, runner=LocalCommandRunner())

    # Display results
    for check in result.checks:
        if check.status == "ok":
            console.print(f"  [green]✓[/green] {check.name}: {check.detail}")
        elif check.status == "warning":
            console.print(f"  [yellow]![/yellow] {check.name}: {check.detail}")
        elif check.status == "error":
            console.print(f"  [red]✗[/red] {check.name}: {check.detail}")
        else:  # info
            console.print(f"  [dim]•[/dim] {check.name}: {check.detail}")

    console.print("")

    # Summary
    if result.overall == "error":
        console.print("[red]Some checks failed[/red]")
        return 1
    if result.overall == "warning":
        console.print("[yellow]Completed with warnings[/yellow]")
        return 0
    console.print("[green]All checks passed[/green]")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:  # noqa: ARG001, C901 - demo flow with dry-run/live modes and feature showcases
    """Demonstrate orchestrator features with mock data.

    Behavior per DEMO_CONTRACT.md:
    - If ISSUE_ORCH_GITHUB_TOKEN is not set: runs dry-run with local fixtures
    - If token set and repo configured: creates demo issue and runs cycle
    """
    import os
    import re

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from ..control.dependency_evaluator import DependencyEvaluator
    from ..control.scheduler import Scheduler
    from ..domain.dependencies import parse_dependencies
    from ..domain.models import AgentConfig, Issue
    from ..infra.config import Config

    console = Console()

    # Check for GitHub token
    token = os.environ.get("ISSUE_ORCH_GITHUB_TOKEN")
    if not token:
        console.print("[bold yellow]DEMO: no token set; running dry-run[/bold yellow]")
        console.print()

    console.print(Panel("[bold cyan]Issue Orchestrator Demo[/bold cyan]", expand=False))
    console.print()

    # Create mock issues using new naming standard: [Mx-nnn][Px-nnn] title
    issues = [
        Issue(
            number=1,
            title="[M1-001][P0-001] Set up project infrastructure",
            labels=["claude"],
            body="External-ID: M1-001\n\nGoal: Set up the basic project structure.",
            milestone="M1",
        ),
        Issue(
            number=2,
            title="[M1-002][P0-010] Add authentication",
            labels=["claude"],
            body="External-ID: M1-002\n\nGoal: Add user authentication.\n\nDepends-on: #1",
            milestone="M1",
        ),
        Issue(
            number=3,
            title="[M1-003][P1-001] Add user dashboard",
            labels=["claude"],
            body="External-ID: M1-003\n\nGoal: Add user dashboard.\n\nDepends-on: #2",
            milestone="M1",
        ),
        Issue(
            number=4,
            title="[M2-001][P2-001] Add reporting feature",
            labels=["claude"],
            body="External-ID: M2-001\n\nGoal: Add reporting.\n\nDepends-on: #3",
            milestone="M2",
        ),
        Issue(
            number=5,
            title="[M1-004][P0-005] Fix critical bug",
            labels=["claude"],
            body="External-ID: M1-004\n\nGoal: Fix critical bug (no dependencies).",
            milestone="M1",
        ),
    ]

    # Show the issues
    console.print("[bold]Demo Issues:[/bold]")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim")
    table.add_column("Title")
    table.add_column("Priority")
    table.add_column("Dependencies")

    for issue in issues:
        deps = parse_dependencies(issue.body or "")
        dep_str = ", ".join(f"#{d[0]}" for d in deps) if deps else "-"
        # Extract priority from title [Px-nnn]
        priority_match = re.search(r"\[P(\d)-\d+\]", issue.title)
        if priority_match:
            p_tier = int(priority_match.group(1))
            priority = f"P{p_tier}"
            color = "red" if p_tier == 0 else "yellow" if p_tier == 1 else "green"
        else:
            priority = "-"
            color = "dim"
        table.add_row(
            str(issue.number),
            issue.title,
            f"[{color}]{priority}[/]",
            dep_str,
        )
    console.print(table)
    console.print()

    class CollectingEventSink:
        """Collects events for display."""

        def __init__(self):
            self.events = []

        def publish(self, event):
            self.events.append(event)

    checker = _DemoIssueChecker()
    events = CollectingEventSink()

    # Create config first so we can use foundation_milestone
    config = Config(
        repo="demo/repo",
        repo_root=Path("."),
        worktree_base=Path("/tmp"),
        agents={"claude": AgentConfig(prompt_path=Path("prompt.txt"))},
        max_concurrent_sessions=2,
    )

    # Create evaluator and scheduler
    evaluator = DependencyEvaluator(
        issue_checker=checker,
        events=events,
        foundation_milestone=config.foundation_milestone,
    )
    scheduler = Scheduler(config=config, dependency_evaluator=evaluator)

    # Show dependency evaluation
    console.print(
        "[bold]Scenario:[/bold] Issue #1 is CLOSED (completed), issues #2-5 are OPEN"
    )
    console.print()

    console.print("[bold]Dependency Evaluation:[/bold]")
    dep_table = Table(show_header=True, header_style="bold magenta")
    dep_table.add_column("#", style="dim")
    dep_table.add_column("Dependencies")
    dep_table.add_column("Status")
    dep_table.add_column("Runnable?")

    for issue in issues:
        report = evaluator.evaluate(
            issue.number, issue.body or "", source_milestone=issue.milestone
        )
        deps = parse_dependencies(issue.body or "")
        dep_str = ", ".join(f"#{d[0]}" for d in deps) if deps else "-"

        if report.runnable:
            status = "[green]All satisfied[/green]"
            runnable = "[green]✓ Yes[/green]"
        else:
            status = f"[red]{report.summary()}[/red]"
            runnable = "[red]✗ No[/red]"

        dep_table.add_row(str(issue.number), dep_str, status, runnable)

    console.print(dep_table)
    console.print()

    # Show scheduling decision
    available, blocked = scheduler.get_available_issues(issues)
    sorted_available = scheduler.sort_by_priority(available)

    console.print("[bold]Scheduling Decision:[/bold]")
    console.print(
        f"  Available issues: {len(available)} (would launch up to {config.max_concurrent_sessions})"
    )
    console.print(f"  Blocked by dependencies: {len(blocked)}")
    console.print()

    if sorted_available:
        console.print("[green]Issues ready to work on (sorted by priority):[/green]")
        for i, issue in enumerate(sorted_available, 1):
            console.print(f"  {i}. #{issue.number}: {issue.title}")
    else:
        console.print("[yellow]No issues available to work on.[/yellow]")

    console.print()

    if blocked:
        console.print("[yellow]Issues blocked by dependencies:[/yellow]")
        for issue, reason in blocked:
            console.print(f"  • #{issue.number}: {reason}")

    console.print()
    console.print(
        Panel(
            "[dim]This demo shows how the orchestrator:\n"
            "1. Uses naming standard: [Mx-nnn][Px-nnn] title\n"
            "   - Mx-nnn = milestone + external ID\n"
            "   - Px-nnn = priority tier (P0 highest) + sequence\n"
            "2. Parses 'Depends-on: #N' lines from issue bodies\n"
            "3. Checks if dependency issues are closed (satisfied)\n"
            "4. Blocks issues with unsatisfied dependencies\n"
            "5. Sorts by: milestone → priority tier → sequence → issue #[/dim]",
            title="Summary",
            expand=False,
        )
    )

    return 0


def cmd_trace(args: argparse.Namespace) -> int:  # noqa: C901 - log parsing with pattern matching and filtering logic
    """Trace log entries for a specific issue."""
    import re

    issue_number = args.issue_number

    # Find the log file by walking up from cwd to find repo root
    def find_log_file() -> Path | None:
        current = Path.cwd()
        for _ in range(10):  # Max 10 levels up
            candidate = (
                current / ".issue-orchestrator" / "state" / "logs" / "orchestrator.log"
            )
            if candidate.exists():
                return candidate
            if current.parent == current:
                break
            current = current.parent
        return None

    log_file = find_log_file()

    if log_file is None:
        console.print("[red]Error: orchestrator.log not found[/red]")
        console.print(
            "Run this command from within a repository that has the orchestrator running."
        )
        return 1

    # Read the log file
    content = log_file.read_text()
    lines = content.splitlines()

    # Find the last startup marker
    last_start = 0
    for i, line in enumerate(lines):
        if "Starting orchestrator" in line:
            last_start = i

    if last_start == 0 and lines:
        console.print(
            "[yellow]Warning: No startup marker found, showing all entries[/yellow]",
            style="dim",
        )

    # Filter entries for this issue
    # Matches: [issue-N] or issue=N or issue_number=N or issue #N
    pattern = re.compile(
        rf"\[issue-{issue_number}\]|"
        rf"issue={issue_number}(?![0-9])|"
        rf"issue_number={issue_number}(?![0-9])|"
        rf"issue #{issue_number}(?![0-9])"
    )

    matches = []
    for line in lines[last_start:]:
        if pattern.search(line):
            matches.append(line)

    if not matches:
        console.print(f"[dim]No log entries found for issue #{issue_number}[/dim]")
        return 0

    for line in matches:
        print(line)

    return 0
