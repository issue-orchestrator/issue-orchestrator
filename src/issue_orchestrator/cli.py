import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()

# Set up logging - writes to file by default, --debug enables console output
LOG_FILE = Path.home() / ".issue-orchestrator.log"


def setup_logging(debug: bool = False) -> None:
    """Configure logging for the application.

    Logs always go to file only (not console) to avoid conflicts with TUI.
    Use `tail -f ~/.issue-orchestrator.log` in another terminal to watch logs.
    """
    # Get root logger and configure it
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Remove any existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add file handler
    file_handler = logging.FileHandler(LOG_FILE, mode='a')
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s'))
    root_logger.addHandler(file_handler)

    logging.info("=" * 50)
    logging.info("issue-orchestrator started (debug=%s)", debug)


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

    # Create 5 test issues (matches scripts/setup_test_issues.py)
    test_issues = [
        ("[TEST] Simple backend task", "agent:backend", "priority:high"),
        ("[TEST] Frontend feature", "agent:frontend", "priority:medium"),
        ("[TEST] Mobile bug fix", "agent:mobile", "priority:low"),
        ("[TEST] Task that will block", "agent:backend", None),
        ("[TEST] Task with dependency", "agent:backend", None),
    ]

    for title, agent_label, priority_label in test_issues:
        # Create labels if needed
        labels_to_create = [agent_label]
        if priority_label:
            labels_to_create.append(priority_label)
        for label in labels_to_create:
            subprocess.run(
                ["gh", "label", "create", label, "--repo", repo, "--force"],
                capture_output=True
            )

        # Build issue create command
        cmd = ["gh", "issue", "create", "--repo", repo, "--title", title,
               "--body", f"Test issue for orchestrator.\n\nExpected: Agent completes.",
               "--label", "test-data", "--label", agent_label]
        if priority_label:
            cmd.extend(["--label", priority_label])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            issue_url = result.stdout.strip()
            console.print(f"  Created: {issue_url}")

    return True


def cmd_start(args: argparse.Namespace) -> int:
    """Start the orchestrator."""
    # Set up logging first
    debug = getattr(args, 'debug', False)
    setup_logging(debug=debug)

    console.print("[green]Starting issue-orchestrator...[/green]")
    if debug:
        console.print(f"[dim]Debug logging enabled (tail -f {LOG_FILE})[/dim]")

    try:
        from .config import Config
        from .orchestrator import Orchestrator
        from .dashboard import run_with_dashboard

        config = Config.find_and_load()
    except FileNotFoundError as e:
        logging.error(f"Config not found: {e}")
        console.print(f"[red]Error: {e}[/red]")
        console.print("Create a .issue-orchestrator.yaml config file first.")
        return 1
    except Exception as e:
        logging.exception(f"Unexpected error loading config: {e}")
        console.print(f"[red]Unexpected error: {e}[/red]")
        return 1

    # Handle test mode
    if args.test_mode:
        if not config.repo:
            console.print("[red]Error: repo must be set in config for test mode[/red]")
            return 1
        _run_test_setup(config.repo)
        config.filter_label = "test-data"
        console.print("[cyan]Test mode: filter_label set to 'test-data'[/cyan]")

    # Handle milestone override
    if hasattr(args, 'milestone') and args.milestone:
        config.filter_milestone = args.milestone
        console.print(f"[cyan]Filtering by milestone: {args.milestone}[/cyan]")

    # Handle ui_mode override
    if hasattr(args, 'ui_mode') and args.ui_mode:
        config.ui_mode = args.ui_mode
    console.print(f"[dim]UI mode: {config.ui_mode}[/dim]")

    console.print(f"[dim]Loaded config with {len(config.agents)} agent types[/dim]")
    console.print(f"[dim]Max concurrent sessions: {config.max_sessions}[/dim]")

    # Handle dry-run mode
    if hasattr(args, 'dry_run') and args.dry_run:
        from .github import list_issues
        from .scheduler import Scheduler
        from .tmux import get_manager
        from .analysis import analyze_all_issues, get_issue_branches

        console.print("\n[cyan]DRY RUN - showing what would be processed:[/cyan]\n")

        scheduler = Scheduler(config)
        tmux_mgr = get_manager()
        all_issues = []

        for agent_label in config.agents.keys():
            labels = [agent_label]
            if config.filter_label:
                labels.append(config.filter_label)
            issues = list_issues(
                config.repo,
                labels=labels,
                milestone=config.filter_milestone,
            )
            all_issues.extend(issues)

        if not all_issues:
            console.print("[yellow]No matching issues found.[/yellow]")
            return 0

        # Analyze all issues using shared logic
        states = analyze_all_issues(
            issues=all_issues,
            repo=config.repo,
            repo_root=config.repo_root,
            check_session_fn=tmux_mgr.window_exists,
        )

        # Sort by priority
        states.sort(key=lambda s: s.issue.priority)

        table = Table(title="All Matching Issues")
        table.add_column("#", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Agent", style="blue")
        table.add_column("Pri", style="magenta", width=4)
        table.add_column("Status", style="yellow")
        table.add_column("Session", style="green")
        table.add_column("Branch", style="cyan")

        for state in states:
            issue = state.issue

            # Status styling
            status = state.status_summary
            status_styles = {
                "available": "green",
                "active": "green",
                "pr-pending": "blue",
                "blocked": "red",
                "needs-human": "red",
                "stale-with-branch": "yellow",
                "stale-orphaned": "yellow",
            }
            style = status_styles.get(status, "white")

            session_status = "[green]active[/green]" if state.has_session else "[dim]none[/dim]"
            branch_status = f"[cyan]{state.branch[:20]}...[/cyan]" if state.branch and len(state.branch) > 20 else f"[cyan]{state.branch}[/cyan]" if state.branch else "[dim]none[/dim]"

            table.add_row(
                str(issue.number),
                issue.title[:35] + ("..." if len(issue.title) > 35 else ""),
                (issue.agent_type or "-").replace("agent:", ""),
                f"P{issue.priority}",
                f"[{style}]{status}[/{style}]",
                session_status,
                branch_status,
            )

        console.print(table)

        # Summary
        available = scheduler.get_available_issues(all_issues)
        console.print(f"\n[dim]Total issues: {len(all_issues)}[/dim]")
        console.print(f"[dim]Available to process: {len(available)}[/dim]")
        console.print(f"[dim]Would launch up to {config.max_sessions} concurrent sessions[/dim]")

        # Warnings for stale issues
        stale_states = [s for s in states if s.is_stale]
        if stale_states:
            console.print(f"\n[yellow]⚠ {len(stale_states)} issue(s) marked in-progress but have no active session:[/yellow]")
            for state in stale_states:
                if state.branch:
                    console.print(f"  [yellow]#{state.issue.number}[/yellow]: {state.issue.title[:35]} [cyan](has branch: {state.branch})[/cyan]")
                else:
                    console.print(f"  [yellow]#{state.issue.number}[/yellow]: {state.issue.title[:40]}")

            console.print("\n[dim]Options:[/dim]")
            console.print("[dim]  • Reset to restart fresh: gh issue edit # --remove-label in-progress[/dim]")
            console.print("[dim]  • Resume from branch: orchestrator will checkout existing branch if present[/dim]")

        # Show issues with branches but not in-progress (might be abandoned PRs)
        from .analysis import analyze_orphan_branches
        issue_branches = get_issue_branches(config.repo_root)
        in_progress_nums = {s.issue.number for s in states if s.issue.is_in_progress}
        orphan_states = analyze_orphan_branches(
            issue_branches, in_progress_nums, config.repo, config.repo_root
        )
        if orphan_states:
            console.print(f"\n[yellow]⚠ {len(orphan_states)} orphan branch(es) found:[/yellow]")

            orphan_table = Table(title=None, box=None)
            orphan_table.add_column("#", style="cyan", width=6)
            orphan_table.add_column("Branch", style="dim")
            orphan_table.add_column("Issue", style="white")
            orphan_table.add_column("Commits", style="magenta", width=7)
            orphan_table.add_column("Age", style="dim", width=12)
            orphan_table.add_column("Action", style="yellow")

            for orphan in orphan_states:
                issue_info = ""
                if orphan.issue_title:
                    title_short = orphan.issue_title[:25] + ("..." if len(orphan.issue_title) > 25 else "")
                    state_color = "green" if orphan.issue_state == "open" else "red"
                    issue_info = f"[{state_color}]{orphan.issue_state}[/{state_color}]: {title_short}"
                elif orphan.issue_state:
                    state_color = "green" if orphan.issue_state == "open" else "red"
                    issue_info = f"[{state_color}]{orphan.issue_state}[/{state_color}]"
                else:
                    issue_info = "[dim]not found[/dim]"

                action_styles = {
                    "resume-work": "[green]resume[/green]",
                    "investigate": "[yellow]investigate[/yellow]",
                    "delete-branch": "[red]delete[/red]",
                }
                action = action_styles.get(orphan.suggested_action, orphan.suggested_action)

                orphan_table.add_row(
                    str(orphan.issue_number),
                    orphan.branch_name[:30] + ("..." if len(orphan.branch_name) > 30 else ""),
                    issue_info,
                    str(orphan.commits_ahead),
                    orphan.last_commit_date or "-",
                    action,
                )

            console.print(orphan_table)

            # Actionable hints
            resume_count = sum(1 for o in orphan_states if o.suggested_action == "resume-work")
            delete_count = sum(1 for o in orphan_states if o.suggested_action == "delete-branch")
            if resume_count > 0:
                console.print(f"\n[dim]To resume work on open issues, add in-progress label:[/dim]")
                console.print(f"[dim]  gh issue edit # --add-label {config.get_label_in_progress()}[/dim]")
            if delete_count > 0:
                console.print(f"\n[dim]To clean up stale branches:[/dim]")
                console.print(f"[dim]  git push origin --delete <branch-name>[/dim]")

        return 0

    # For iterm2 mode, run directly (no tmux wrapper) - agent sessions become iTerm2 tabs
    if config.ui_mode == "iterm2":
        import subprocess
        from .iterm2 import is_running_in_iterm2

        # If not in iTerm2, launch iTerm2 with our command
        if not is_running_in_iterm2():
            import sys
            # Get the full path to the Python interpreter (in venv)
            python_path = sys.executable
            # Get the repo root directory where config lives
            working_dir = str(config.repo_root)
            # Build the command using python -m to ensure venv is used
            cmd_args = [python_path, "-m", "issue_orchestrator.cli", "start"]
            if args.test_mode:
                cmd_args.append("--test-mode")
            if args.debug:
                cmd_args.append("--debug")
            if args.no_dashboard:
                cmd_args.append("--no-dashboard")
            cmd_args.extend(["--ui-mode", "iterm2"])
            cmd_str = f"cd {working_dir} && " + " ".join(cmd_args)

            console.print("[dim]Launching iTerm2...[/dim]")
            # Use AppleScript to open iTerm2 and run our command directly (no tmux)
            applescript = f'''tell application "iTerm"
activate
if (count of windows) = 0 then
create window with default profile
end if
tell current window
set newTab to (create tab with default profile)
tell current session of newTab
write text "{cmd_str}"
end tell
end tell
end tell'''
            subprocess.run(["osascript", "-e", applescript])
            console.print("[green]iTerm2 launched. Switch to iTerm2 to see the dashboard.[/green]")
            return 0

        # We're in iTerm2 - just continue and run dashboard directly
        console.print("[dim]Running in iTerm2 native mode (no tmux)[/dim]")

    orchestrator = Orchestrator(config=config)

    # Run startup to clean up stale issues
    asyncio.run(orchestrator.startup())

    try:
        if args.no_dashboard:
            # Run orchestrator without dashboard (useful for CI/debugging)
            console.print("[dim]Running without dashboard UI[/dim]")
            asyncio.run(orchestrator.run_loop())
        elif config.ui_mode == "web":
            # Run with web dashboard in browser
            from .web import run_with_web_dashboard
            console.print("[dim]Starting web dashboard...[/dim]")
            console.print("[green]Dashboard will open in your browser[/green]")
            asyncio.run(run_with_web_dashboard(orchestrator, port=8080))
        else:
            # Run with interactive TUI dashboard (tmux or iterm2 mode)
            should_attach = asyncio.run(run_with_dashboard(orchestrator, config.ui_mode))
            if should_attach:
                # User pressed 1-9 to attach to a session - already in tmux for iterm2 mode
                if config.ui_mode != "iterm2":
                    os.execvp("tmux", ["tmux", "attach-session", "-t", "orchestrator"])
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show current status."""
    try:
        from .config import Config
        from .tmux import list_sessions

        config = Config.find_and_load()

        # Get active tmux sessions that look like ours
        all_sessions = list_sessions()
        our_sessions = [s for s in all_sessions if s.startswith("issue-")]

        console.print("\n[cyan]Orchestrator Status[/cyan]")
        console.print(f"\n[bold]Config:[/bold]")
        console.print(f"  Repo: {config.repo or '(auto-detect)'}")
        console.print(f"  Max sessions: {config.max_sessions}")
        console.print(f"  Agents: {', '.join(config.agents.keys())}")
        if config.filter_label:
            console.print(f"  Filter label: {config.filter_label}")
        if config.filter_milestone:
            console.print(f"  Filter milestone: {config.filter_milestone}")

        console.print(f"\n[bold]Active Sessions ({len(our_sessions)}):[/bold]")
        if our_sessions:
            for session in our_sessions:
                issue_num = session.replace("issue-", "")
                console.print(f"  • #{issue_num} ({session})")
        else:
            console.print("  (none)")

        return 0
    except FileNotFoundError:
        console.print("[yellow]Orchestrator not configured yet[/yellow]")
        return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach to the orchestrator tmux session."""
    from .tmux import attach_session, get_manager

    manager = get_manager()
    if not manager.has_session():
        console.print("[red]No orchestrator session running[/red]")
        return 1

    # If issue number provided, switch to that window first
    if hasattr(args, 'issue_number') and args.issue_number:
        if not manager.select_window(args.issue_number):
            console.print(f"[yellow]Window for issue #{args.issue_number} not found[/yellow]")

    attach_session("")  # Attaches to orchestrator session
    return 0  # Never reached if attach succeeds


def cmd_switch(args: argparse.Namespace) -> int:
    """Switch to a specific issue window (when inside tmux)."""
    from .tmux import get_manager

    manager = get_manager()
    if not manager.has_session():
        console.print("[red]No orchestrator session running[/red]")
        return 1

    issue_number: int = args.issue_number
    if manager.select_window(issue_number):
        console.print(f"[green]Switched to issue #{issue_number}[/green]")
        return 0
    else:
        console.print(f"[red]No window for issue #{issue_number}[/red]")
        return 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Switch to the dashboard window (when inside tmux)."""
    from .tmux import get_manager

    manager = get_manager()
    if not manager.has_session():
        console.print("[red]No orchestrator session running[/red]")
        return 1

    if manager.select_dashboard():
        console.print("[green]Switched to dashboard[/green]")
        return 0
    else:
        console.print("[red]Dashboard window not found[/red]")
        return 1


def cmd_output(args: argparse.Namespace) -> int:
    """Show recent output from an issue's session."""
    from .tmux import get_manager

    manager = get_manager()
    issue_number: int = args.issue_number
    lines: int = getattr(args, 'lines', 20)

    output = manager.capture_pane_output(issue_number, lines=lines)
    if output is None:
        console.print(f"[red]No window for issue #{issue_number}[/red]")
        return 1

    console.print(f"[cyan]Output from issue #{issue_number}:[/cyan]\n")
    console.print(output)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    """Pause the orchestrator."""
    from .locks import set_paused, is_paused

    if is_paused():
        console.print("[yellow]Orchestrator is already paused[/yellow]")
        return 0

    set_paused()
    console.print("[yellow]Orchestrator paused. Current sessions will finish, but no new issues will be started.[/yellow]")
    console.print("[dim]Run 'issue-orchestrator resume' to continue processing issues.[/dim]")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume the orchestrator."""
    from .locks import set_resumed, is_paused

    if not is_paused():
        console.print("[yellow]Orchestrator is not paused[/yellow]")
        return 0

    set_resumed()
    console.print("[green]Orchestrator resumed. Will continue processing issues.[/green]")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    """Prioritize an issue."""
    issue_number: int = args.issue_number
    console.print(
        f"[cyan]Prioritizing issue #{issue_number}...[/cyan]"
    )
    # TODO: implement prioritization logic
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize required GitHub labels."""
    import subprocess

    try:
        from .config import Config

        config = Config.find_and_load()
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("Create a .issue-orchestrator.yaml config file first.")
        return 1

    repo = config.repo
    if not repo:
        console.print("[red]Error: repo must be set in config[/red]")
        return 1

    console.print(f"[cyan]Initializing labels for {repo}...[/cyan]\n")

    # Collect all labels to create
    labels = [
        config.get_label_in_progress(),
        config.get_label_blocked(),
        config.get_label_needs_human(),
        "priority:high",
        "priority:medium",
        "priority:low",
    ]
    # Add all agent labels from config
    labels.extend(config.agents.keys())

    created = 0
    updated = 0
    failed = 0

    for label in labels:
        result = subprocess.run(
            ["gh", "label", "create", label, "--repo", repo, "--force"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            # Check if it was created or updated
            if "already exists" in result.stderr.lower() or result.stderr:
                console.print(f"  [yellow]↻[/yellow] {label}")
                updated += 1
            else:
                console.print(f"  [green]✓[/green] {label}")
                created += 1
        else:
            console.print(f"  [red]✗[/red] {label}: {result.stderr.strip()}")
            failed += 1

    # Print summary
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Created: {created}")
    console.print(f"  Updated: {updated}")
    console.print(f"  Failed: {failed}")

    if failed > 0:
        console.print("\n[yellow]Some labels failed to create. Check your gh CLI auth.[/yellow]")
        return 1

    console.print("\n[green]✓ Label initialization complete![/green]")
    return 0


def cmd_test_reset(args: argparse.Namespace) -> int:
    """Reset test environment: teardown + setup."""
    import subprocess
    import sys
    from pathlib import Path

    console.print("[bold]Test Reset: Clean slate for integration testing[/bold]\n")

    # Find the scripts directory
    scripts_dir = Path(__file__).parent.parent.parent / "scripts"
    if not scripts_dir.exists():
        # Try installed package location
        scripts_dir = Path(__file__).parent / "scripts"

    if not scripts_dir.exists():
        console.print("[red]Error: scripts directory not found[/red]")
        return 1

    # Step 1: Teardown
    console.print("[cyan]Step 1: Tearing down existing test data...[/cyan]")
    teardown_script = scripts_dir / "teardown_test_issues.py"
    if teardown_script.exists():
        result = subprocess.run([sys.executable, str(teardown_script)])
        if result.returncode != 0:
            console.print("[yellow]Warning: Teardown had issues, continuing...[/yellow]")
    else:
        console.print("[yellow]Teardown script not found, skipping...[/yellow]")

    console.print()

    # Step 2: Setup
    console.print("[cyan]Step 2: Creating fresh test issues...[/cyan]")
    setup_script = scripts_dir / "setup_test_issues.py"
    if setup_script.exists():
        result = subprocess.run([sys.executable, str(setup_script)])
        if result.returncode != 0:
            console.print("[red]Error: Setup failed![/red]")
            return 1
    else:
        console.print("[yellow]Setup script not found, skipping...[/yellow]")

    console.print()
    console.print("[green]✓ Test reset complete![/green]")
    console.print("\nNow run: [bold]issue-orchestrator start[/bold]")
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
    start_parser.add_argument(
        "--milestone",
        type=str,
        default=None,
        help="Filter issues by milestone name"
    )
    start_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what issues would be processed without launching sessions"
    )
    start_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose DEBUG-level logging to ~/.issue-orchestrator.log"
    )
    start_parser.add_argument(
        "--ui-mode",
        choices=["tmux", "iterm2", "web"],
        default=None,
        help="UI mode: tmux (default), iterm2 (Mac tabs), or web (browser dashboard)"
    )
    start_parser.set_defaults(func=cmd_start)

    # status command
    status_parser: argparse.ArgumentParser = subparsers.add_parser(
        "status", help="Show current status"
    )
    status_parser.set_defaults(func=cmd_status)

    # attach command
    attach_parser: argparse.ArgumentParser = subparsers.add_parser(
        "attach", help="Attach to the orchestrator tmux session"
    )
    attach_parser.add_argument(
        "issue_number",
        type=int,
        nargs="?",
        default=None,
        help="Optional: switch to this issue's window after attaching"
    )
    attach_parser.set_defaults(func=cmd_attach)

    # switch command
    switch_parser: argparse.ArgumentParser = subparsers.add_parser(
        "switch", help="Switch to an issue's window (when inside tmux)"
    )
    switch_parser.add_argument(
        "issue_number",
        type=int,
        help="GitHub issue number to switch to"
    )
    switch_parser.set_defaults(func=cmd_switch)

    # dashboard command
    dashboard_parser: argparse.ArgumentParser = subparsers.add_parser(
        "dashboard", help="Switch to the dashboard window (when inside tmux)"
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    # output command
    output_parser: argparse.ArgumentParser = subparsers.add_parser(
        "output", help="Show recent output from an issue's session"
    )
    output_parser.add_argument(
        "issue_number",
        type=int,
        help="GitHub issue number"
    )
    output_parser.add_argument(
        "-n", "--lines",
        type=int,
        default=20,
        help="Number of lines to show (default: 20)"
    )
    output_parser.set_defaults(func=cmd_output)

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

    # init command
    init_parser: argparse.ArgumentParser = subparsers.add_parser(
        "init", help="Initialize required GitHub labels"
    )
    init_parser.set_defaults(func=cmd_init)

    # test-reset command
    reset_parser: argparse.ArgumentParser = subparsers.add_parser(
        "test-reset", help="Reset test environment (teardown + setup)"
    )
    reset_parser.set_defaults(func=cmd_test_reset)

    args: argparse.Namespace = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
