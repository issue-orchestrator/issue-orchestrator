import argparse
import asyncio
from typing import Any

from rich.console import Console

console = Console()


def _run_test_setup(repo: str) -> bool:
    """Run test teardown and setup. Returns True on success."""
    import subprocess

    console.print("[cyan]Test mode: Cleaning up old test issues...[/cyan]")

    # Teardown
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--label", "test-data",
         "--state", "open", "--json", "number"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        import json
        issues = json.loads(result.stdout)
        for issue in issues:
            subprocess.run(
                ["gh", "issue", "close", str(issue["number"]), "--repo", repo,
                 "--comment", "Closed by test mode startup."],
                capture_output=True
            )
            console.print(f"  Closed #{issue['number']}")

    console.print("[cyan]Test mode: Creating fresh test issues...[/cyan]")

    # Create test-data label if missing
    subprocess.run(
        ["gh", "label", "create", "test-data", "--repo", repo, "--force",
         "--description", "Test data for integration tests"],
        capture_output=True
    )

    # Create test issues
    test_issues = [
        ("[TEST] Simple backend task", "agent:backend", "priority:high"),
        ("[TEST] Frontend feature", "agent:frontend", "priority:medium"),
        ("[TEST] Mobile bug fix", "agent:mobile", "priority:low"),
    ]

    for title, agent_label, priority_label in test_issues:
        # Create labels if needed
        for label in [agent_label, priority_label]:
            subprocess.run(
                ["gh", "label", "create", label, "--repo", repo, "--force"],
                capture_output=True
            )

        result = subprocess.run(
            ["gh", "issue", "create", "--repo", repo, "--title", title,
             "--body", f"Test issue for orchestrator.\n\nExpected: Agent completes.",
             "--label", "test-data", "--label", agent_label, "--label", priority_label],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            issue_url = result.stdout.strip()
            console.print(f"  Created: {issue_url}")

    return True


def cmd_start(args: argparse.Namespace) -> int:
    """Start the orchestrator."""
    console.print("[green]Starting issue-orchestrator...[/green]")

    try:
        from .config import Config
        from .orchestrator import Orchestrator
        from .dashboard import run_with_dashboard

        config = Config.find_and_load()
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("Create a .issue-orchestrator.yaml config file first.")
        return 1

    # Handle test mode
    if args.test_mode:
        if not config.repo:
            console.print("[red]Error: repo must be set in config for test mode[/red]")
            return 1
        _run_test_setup(config.repo)
        config.filter_label = "test-data"
        console.print("[cyan]Test mode: filter_label set to 'test-data'[/cyan]")

    console.print(f"[dim]Loaded config with {len(config.agents)} agent types[/dim]")
    console.print(f"[dim]Max concurrent sessions: {config.max_sessions}[/dim]")

    orchestrator = Orchestrator(config=config)

    try:
        if args.no_dashboard:
            # Run orchestrator without dashboard (useful for CI/debugging)
            console.print("[dim]Running without dashboard UI[/dim]")
            asyncio.run(orchestrator.run())
        else:
            # Run with interactive dashboard
            asyncio.run(run_with_dashboard(orchestrator))
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show current status."""
    try:
        from .orchestrator import Orchestrator
        from .config import Config

        config = Config.find_and_load()
        orchestrator = Orchestrator(config=config)

        # Get current state
        state = orchestrator.get_state()

        console.print("\n[cyan]Orchestrator Status[/cyan]")
        console.print(f"  Active sessions: {len(state.get('active_sessions', []))}")
        console.print(f"  Queued issues: {len(state.get('queued_issues', []))}")
        console.print(f"  Completed: {state.get('completed_count', 0)}")

        return 0
    except FileNotFoundError:
        console.print("[yellow]Orchestrator not configured yet[/yellow]")
        return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach to a running session."""
    from .tmux import attach_session

    issue_number: int = args.issue_number
    session_name: str = f"issue-{issue_number}"
    attach_session(session_name)
    return 0  # Never reached if attach succeeds


def cmd_pause(args: argparse.Namespace) -> int:
    """Pause the orchestrator."""
    console.print("[yellow]Pausing issue-orchestrator...[/yellow]")
    # TODO: implement pause logic
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume the orchestrator."""
    console.print("[green]Resuming issue-orchestrator...[/green]")
    # TODO: implement resume logic
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    """Prioritize an issue."""
    issue_number: int = args.issue_number
    console.print(
        f"[cyan]Prioritizing issue #{issue_number}...[/cyan]"
    )
    # TODO: implement prioritization logic
    return 0


def main() -> int:
    """Main entry point for the CLI."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Orchestrate AI agents working on GitHub issues"
    )
    subparsers: Any = parser.add_subparsers(
        dest="command", required=True
    )

    # start command
    start_parser: argparse.ArgumentParser = subparsers.add_parser(
        "start", help="Start the orchestrator"
    )
    start_parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run without dashboard UI (useful for CI/debugging)"
    )
    start_parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Clear test issues, create fresh ones, and run with filter_label=test-data"
    )
    start_parser.set_defaults(func=cmd_start)

    # status command
    status_parser: argparse.ArgumentParser = subparsers.add_parser(
        "status", help="Show current status"
    )
    status_parser.set_defaults(func=cmd_status)

    # attach command
    attach_parser: argparse.ArgumentParser = subparsers.add_parser(
        "attach", help="Attach to a running session"
    )
    attach_parser.add_argument(
        "issue_number",
        type=int,
        help="GitHub issue number to attach to"
    )
    attach_parser.set_defaults(func=cmd_attach)

    # pause command
    pause_parser: argparse.ArgumentParser = subparsers.add_parser(
        "pause", help="Pause the orchestrator"
    )
    pause_parser.set_defaults(func=cmd_pause)

    # resume command
    resume_parser: argparse.ArgumentParser = subparsers.add_parser(
        "resume", help="Resume the orchestrator"
    )
    resume_parser.set_defaults(func=cmd_resume)

    # next command
    next_parser: argparse.ArgumentParser = subparsers.add_parser(
        "next", help="Prioritize an issue"
    )
    next_parser.add_argument(
        "issue_number",
        type=int,
        help="GitHub issue number to prioritize"
    )
    next_parser.set_defaults(func=cmd_next)

    args: argparse.Namespace = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
