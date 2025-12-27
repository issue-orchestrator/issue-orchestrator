import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .orchestrator import Orchestrator

from rich.console import Console
from rich.table import Table

from .logging_config import setup_logging

console = Console()

# Re-export LOG_FILE for backward compatibility (e.g., --show-logs command)
LOG_FILE = Path.home() / ".issue-orchestrator.log"


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
    no_dashboard = getattr(args, 'no_dashboard', False)
    # Enable console output when not using dashboard (safe to log to stderr)
    setup_logging(level="DEBUG" if debug else "INFO", console_output=no_dashboard)

    console.print("[green]Starting issue-orchestrator...[/green]")
    if debug:
        console.print(f"[dim]Debug logging enabled (tail -f {LOG_FILE})[/dim]")

    try:
        from .bootstrap import build_orchestrator
        from .dashboard import run_with_dashboard

        config = _load_config(args)

        # Validate configuration early - fail fast with clear errors
        validation_errors = config.validate()
        if validation_errors:
            console.print("[red]Configuration errors:[/red]")
            for error in validation_errors:
                console.print(f"  [red]• {error}[/red]")
                logging.error(f"Config validation: {error}")
            return 1

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

    # Handle label override
    if hasattr(args, 'label') and args.label:
        config.filter_label = args.label
        console.print(f"[cyan]Filtering by label: {args.label}[/cyan]")

    # Handle single issue filter
    if hasattr(args, 'issue') and args.issue:
        config.filter_issue = args.issue
        console.print(f"[cyan]Processing only issue #{args.issue}[/cyan]")

    # Handle ui_mode override
    if hasattr(args, 'ui_mode') and args.ui_mode:
        config.ui_mode = args.ui_mode
    console.print(f"[dim]UI mode: {config.ui_mode}[/dim]")

    # Handle queue_refresh override
    if hasattr(args, 'queue_refresh') and args.queue_refresh is not None:
        config.queue_refresh_seconds = args.queue_refresh

    # Handle max_issues override
    if hasattr(args, 'max_issues') and args.max_issues is not None:
        config.max_issues_to_start = args.max_issues
        if config.max_issues_to_start > 0:
            console.print(f"[dim]Max issues to start: {config.max_issues_to_start}[/dim]")

    # Handle review workflow overrides
    if hasattr(args, 'review_label') and args.review_label is not None:
        config.triage_review_label = args.review_label
        console.print(f"[dim]Review label: {config.triage_review_label}[/dim]")
    if hasattr(args, 'review_threshold') and args.review_threshold is not None:
        config.triage_review_threshold = args.review_threshold
        if config.triage_review_threshold > 0:
            console.print(f"[dim]Review threshold: {config.triage_review_threshold} PRs[/dim]")

    console.print(f"[dim]Loaded config with {len(config.agents)} agent types[/dim]")
    console.print(f"[dim]Max concurrent sessions: {config.max_concurrent_sessions}[/dim]")

    # Handle dry-run mode
    if hasattr(args, 'dry_run') and args.dry_run:
        from ._github_impl import list_issues
        from .control.scheduler import Scheduler
        from ._tmux_impl import get_manager
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
                limit=config.issue_fetch_limit,
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
        available, dep_blocked = scheduler.get_available_issues(all_issues, check_dependencies=False)
        console.print(f"\n[dim]Total issues: {len(all_issues)}[/dim]")
        console.print(f"[dim]Available to process: {len(available)}[/dim]")
        console.print(f"[dim]Would launch up to {config.max_concurrent_sessions} concurrent sessions[/dim]")

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
        from ._iterm2_impl import is_running_in_iterm2

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

    orchestrator = build_orchestrator(config=config)

    # Adopt existing sessions if requested (for restart)
    if getattr(args, 'adopt_sessions', False) and config.ui_mode == "iterm2":
        _adopt_iterm2_sessions(orchestrator, config)

    # Run startup to clean up stale issues (skip for web mode - it runs startup in background)
    if config.ui_mode != "web":
        asyncio.run(orchestrator.startup())

    try:
        if args.no_dashboard:
            # Run orchestrator without dashboard (useful for CI/debugging)
            console.print("[dim]Running without dashboard UI[/dim]")
            asyncio.run(orchestrator.run_loop())
        elif config.ui_mode == "web":
            # Run with web dashboard in browser
            from .web import run_with_web_dashboard
            # CLI --port overrides config web_port
            port = args.port if args.port != 8080 else config.web_port
            console.print("[dim]Starting web dashboard...[/dim]")
            console.print(f"[green]Dashboard will open at http://localhost:{port}[/green]")

            # Wrapper to set up signal handlers inside asyncio context
            # (asyncio.run() overwrites signal handlers set before it)
            async def run_with_signals():
                import signal
                from .web import trigger_server_shutdown

                def handle_signal():
                    if orchestrator._shutdown_requested:
                        # Second signal - force kill
                        orchestrator.request_shutdown(force=True)
                        trigger_server_shutdown()
                    else:
                        # First signal - graceful shutdown
                        orchestrator.request_shutdown()
                        trigger_server_shutdown()

                loop = asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGINT, handle_signal)
                loop.add_signal_handler(signal.SIGTERM, handle_signal)

                await run_with_web_dashboard(orchestrator, port=port)

            asyncio.run(run_with_signals())
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
        from ._tmux_impl import list_sessions

        config = _load_config(args)

        # Get active tmux sessions that look like ours
        all_sessions = list_sessions()
        our_sessions = [s for s in all_sessions if s.startswith("issue-")]

        console.print("\n[cyan]Orchestrator Status[/cyan]")
        console.print(f"\n[bold]Config:[/bold]")
        console.print(f"  Repo: {config.repo or '(auto-detect)'}")
        console.print(f"  Max sessions: {config.max_concurrent_sessions}")
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
    from ._tmux_impl import attach_session, get_manager

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
    from ._tmux_impl import get_manager

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
    from ._tmux_impl import get_manager

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
    from ._tmux_impl import get_manager

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
    console.print("[yellow]Pausing issue-orchestrator...[/yellow]")
    # TODO: implement pause logic
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume the orchestrator."""
    console.print("[green]Resuming issue-orchestrator...[/green]")
    # TODO: implement resume logic
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Request immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the queue_refresh_seconds interval. Useful after creating new
    issues or changing labels.
    """
    import httpx

    port = args.port or 8080
    base_url = f"http://localhost:{port}"

    try:
        response = httpx.post(f"{base_url}/api/refresh", timeout=5.0)
        if response.status_code == 200:
            console.print("[green]Refresh requested - issues will be fetched on next loop iteration[/green]")
            return 0
        else:
            console.print(f"[red]Failed to request refresh: {response.text}[/red]")
            return 1
    except httpx.ConnectError:
        console.print("[red]Could not connect to orchestrator. Is it running?[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Error requesting refresh: {e}[/red]")
        return 1


def cmd_restart(args: argparse.Namespace) -> int:
    """Restart the orchestrator, preserving existing iTerm2 sessions.

    This command:
    1. Sends shutdown to the running orchestrator (via API)
    2. Waits for it to exit
    3. Starts a new orchestrator with --adopt-sessions flag
    """
    import subprocess
    import sys
    import time
    import httpx

    port = args.port or 8080
    base_url = f"http://localhost:{port}"

    # Step 1: Check if orchestrator is running
    console.print("[cyan]Checking for running orchestrator...[/cyan]")
    try:
        resp = httpx.get(f"{base_url}/api/status", timeout=2.0)
        if resp.status_code == 200:
            console.print(f"[green]Found orchestrator on port {port}[/green]")
        else:
            console.print(f"[yellow]Orchestrator responded with {resp.status_code}[/yellow]")
    except httpx.ConnectError:
        console.print(f"[yellow]No orchestrator running on port {port}[/yellow]")
        console.print("[cyan]Starting fresh with --adopt-sessions...[/cyan]")
        # Just start fresh
        return _start_with_adopt_sessions(args)

    # Step 2: Send shutdown request
    console.print("[cyan]Sending shutdown request...[/cyan]")
    try:
        resp = httpx.post(f"{base_url}/api/shutdown", timeout=5.0)
        if resp.status_code == 200:
            console.print("[green]Shutdown request accepted[/green]")
        else:
            console.print(f"[yellow]Shutdown returned {resp.status_code}[/yellow]")
    except Exception as e:
        console.print(f"[yellow]Error sending shutdown: {e}[/yellow]")

    # Step 3: Wait for orchestrator to exit (poll the port)
    console.print("[cyan]Waiting for orchestrator to exit...[/cyan]")
    for i in range(30):  # Wait up to 30 seconds
        try:
            httpx.get(f"{base_url}/api/status", timeout=1.0)
            # Still running
            time.sleep(1)
            if i % 5 == 4:
                console.print(f"[dim]Still waiting... ({i+1}s)[/dim]")
        except httpx.ConnectError:
            # Orchestrator has exited
            console.print("[green]Orchestrator stopped[/green]")
            break
    else:
        console.print("[yellow]Orchestrator didn't stop in time, continuing anyway...[/yellow]")

    # Step 4: Start new orchestrator with --adopt-sessions
    console.print("[cyan]Starting new orchestrator with session adoption...[/cyan]")
    return _start_with_adopt_sessions(args)


def _start_with_adopt_sessions(args: argparse.Namespace) -> int:
    """Start orchestrator with --adopt-sessions flag."""
    import subprocess
    import sys

    # Build command to run start with adopt-sessions
    cmd = [sys.executable, "-m", "issue_orchestrator.cli", "start", "--adopt-sessions"]

    # Pass through relevant flags
    if hasattr(args, 'config') and args.config:
        cmd.extend(["--config", args.config])
    if hasattr(args, 'port') and args.port:
        cmd.extend(["--port", str(args.port)])
    if hasattr(args, 'debug') and args.debug:
        cmd.append("--debug")
    if hasattr(args, 'ui_mode') and args.ui_mode:
        cmd.extend(["--ui-mode", args.ui_mode])

    console.print(f"[dim]Running: {' '.join(cmd)}[/dim]")

    # Replace this process with the new orchestrator
    import os
    os.execvp(cmd[0], cmd)
    # execvp doesn't return on success
    return 1


def cmd_next(args: argparse.Namespace) -> int:
    """Prioritize an issue."""
    issue_number: int = args.issue_number
    console.print(
        f"[cyan]Prioritizing issue #{issue_number}...[/cyan]"
    )
    # TODO: implement prioritization logic
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the interactive setup wizard."""
    from pathlib import Path

    from .setup_wizard import run_wizard

    target_path = Path(args.path).expanduser().resolve() if args.path else None
    run_wizard(target_path)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize required GitHub labels."""
    import subprocess

    try:
        config = _load_config(args)
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


def _adopt_iterm2_sessions(orchestrator: "Orchestrator", config: "Config") -> None:
    """Discover and adopt existing iTerm2 tabs as active sessions.

    This allows restarting the orchestrator without losing track of
    running Claude sessions.
    """
    from ._iterm2_impl import discover_issue_tabs, get_iterm_manager
    from .models import Session, Issue
    from datetime import datetime
    import subprocess
    import json

    console.print("[cyan]Discovering existing iTerm2 sessions...[/cyan]")

    issue_numbers = discover_issue_tabs()
    if not issue_numbers:
        console.print("[dim]No existing issue tabs found[/dim]")
        return

    console.print(f"[green]Found {len(issue_numbers)} issue tab(s): {issue_numbers}[/green]")

    # Get the iTerm manager and register discovered sessions
    iterm_mgr = get_iterm_manager()

    adopted = 0
    for issue_num in issue_numbers:
        # Try to find the worktree
        repo_name = config.repo_root.name
        worktree_path = config.repo_root.parent / f"{repo_name}-{issue_num}"

        if not worktree_path.exists():
            console.print(f"  [yellow]#{issue_num}: No worktree found, skipping[/yellow]")
            continue

        # Try to get issue info from GitHub using gh CLI
        try:
            gh_args = ["gh", "issue", "view", str(issue_num), "--json", "title,labels,state"]
            if config.repo:
                gh_args.extend(["--repo", config.repo])
            gh_result = subprocess.run(gh_args, capture_output=True, text=True)
            if gh_result.returncode == 0:
                issue_data = json.loads(gh_result.stdout)
                issue = Issue(
                    number=issue_num,
                    title=issue_data.get("title", f"Issue #{issue_num}"),
                    labels=[lbl["name"] for lbl in issue_data.get("labels", [])],
                    state=issue_data.get("state", "open"),
                )
            else:
                raise RuntimeError(gh_result.stderr)
        except Exception as e:
            console.print(f"  [yellow]#{issue_num}: Couldn't fetch issue: {e}[/yellow]")
            issue = Issue(number=issue_num, title=f"Issue #{issue_num}", labels=[])

        # Find agent config for this issue
        agent_type = issue.agent_type
        if not agent_type or agent_type not in config.agents:
            # Default to first agent
            agent_type = list(config.agents.keys())[0] if config.agents else None

        if not agent_type:
            console.print(f"  [yellow]#{issue_num}: No agent config, skipping[/yellow]")
            continue

        agent_config = config.agents[agent_type]

        # Get branch name from worktree
        import subprocess
        branch_result = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True
        )
        branch_name = branch_result.stdout.strip() if branch_result.returncode == 0 else f"{issue_num}-unknown"

        # Create Session object
        session = Session(
            issue=issue,
            agent_config=agent_config,
            tmux_session_name=f"issue-{issue_num}",  # Convention for phase detection
            worktree_path=worktree_path,
            branch_name=branch_name,
            started_at=datetime.now(),  # We don't know real start time
        )

        # Add to orchestrator's active sessions
        orchestrator.state.active_sessions.append(session)

        # Register with iTerm manager so it can track the session
        iterm_mgr._sessions[issue_num] = {
            "tab_name": f"#{issue_num}",
            "created_at": "adopted",
        }

        console.print(f"  [green]#{issue_num}: Adopted ({agent_type})[/green]")
        adopted += 1

    console.print(f"[green]Adopted {adopted} session(s)[/green]")


def _load_config(args: argparse.Namespace) -> "Config":
    """Load config from explicit path or search for it.

    Args:
        args: Parsed command line arguments

    Returns:
        Loaded Config object

    Raises:
        FileNotFoundError: If config file not found
    """
    from .config import Config

    if hasattr(args, 'config') and args.config:
        config_path = Path(args.config)
        config = Config.load(config_path)
        # Set repo_root to config file's parent directory
        config.repo_root = config_path.parent.resolve()
        return config
    else:
        return Config.find_and_load()


def cmd_audit(args: argparse.Namespace) -> int:
    """Audit the queue - show why issues are queued or skipped."""
    from .audit import audit_queue, print_audit
    from .config import Config
    from .execution.github_adapter import GitHubAdapter

    console.print("[bold]Queue Audit[/bold]\n")

    # Load config
    try:
        if hasattr(args, 'config') and args.config:
            config = Config.load(args.config)
            config.repo_root = args.config.parent.resolve()
        else:
            config = Config.find_and_load()
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1

    console.print(f"[dim]Repository: {config.repo}[/dim]")
    console.print(f"[dim]Agents: {', '.join(config.agents.keys())}[/dim]")

    # Run audit (no state = fresh start, no session history)
    issue_tracker = GitHubAdapter()
    entries = audit_queue(config, state=None, issue_tracker=issue_tracker)
    print_audit(entries)

    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify the orchestrator setup works correctly."""
    import subprocess
    import shutil

    console.print("[bold cyan]Orchestrator Setup Verification[/bold cyan]\n")

    errors = []
    warnings = []

    # 1. Check config file
    console.print("[bold]1. Configuration[/bold]")
    try:
        config = _load_config(args)
        console.print(f"  [green]✓[/green] Config file found")
        console.print(f"    Repo: {config.repo or '(auto-detect)'}")
        console.print(f"    Agents: {', '.join(config.agents.keys())}")
        console.print(f"    Repo root: {config.repo_root}")
    except FileNotFoundError as e:
        console.print(f"  [red]✗[/red] Config not found: {e}")
        errors.append("Config file not found - run 'issue-orchestrator setup'")
        # Can't continue without config
        console.print(f"\n[bold red]Verification failed: {len(errors)} error(s)[/bold red]")
        return 1

    # 2. Check git repository
    console.print("\n[bold]2. Git Repository[/bold]")
    git_check = subprocess.run(
        ["git", "-C", str(config.repo_root), "rev-parse", "--git-dir"],
        capture_output=True, text=True
    )
    if git_check.returncode == 0:
        console.print(f"  [green]✓[/green] Valid git repository")
    else:
        console.print(f"  [red]✗[/red] Not a git repository: {config.repo_root}")
        errors.append("Not a git repository")

    # 3. Check GitHub CLI
    console.print("\n[bold]3. GitHub CLI[/bold]")
    gh_path = shutil.which("gh")
    if gh_path:
        console.print(f"  [green]✓[/green] gh CLI found: {gh_path}")
        # Check authentication
        auth_check = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True
        )
        if auth_check.returncode == 0:
            console.print(f"  [green]✓[/green] gh authenticated")
        else:
            console.print(f"  [red]✗[/red] gh not authenticated")
            errors.append("GitHub CLI not authenticated - run 'gh auth login'")
    else:
        console.print(f"  [red]✗[/red] gh CLI not found")
        errors.append("GitHub CLI not installed")

    # 4. Check hooks setup
    console.print("\n[bold]4. Git Hooks[/bold]")
    from ._worktree_impl import HOOKS_DIR

    bundled_hook = HOOKS_DIR / "pre-push"
    if bundled_hook.exists():
        console.print(f"  [green]✓[/green] Bundled pre-push hook exists")
    else:
        console.print(f"  [red]✗[/red] Bundled pre-push hook missing: {bundled_hook}")
        errors.append("Bundled pre-push hook not found")

    # Check if project uses custom hooksPath
    hooks_path_check = subprocess.run(
        ["git", "-C", str(config.repo_root), "config", "--get", "core.hooksPath"],
        capture_output=True, text=True
    )
    if hooks_path_check.returncode == 0:
        custom_path = hooks_path_check.stdout.strip()
        console.print(f"  [cyan]ℹ[/cyan] Project uses custom hooksPath: {custom_path}")
        project_hook = config.repo_root / custom_path / "pre-push"
        if project_hook.exists():
            console.print(f"  [green]✓[/green] Project pre-push hook found")
            console.print(f"  [cyan]ℹ[/cyan] Hooks will be chained in worktrees")
        else:
            console.print(f"  [yellow]![/yellow] No project pre-push hook at {project_hook}")
            warnings.append("No project pre-push hook found (chaining not needed)")
    else:
        # Check standard hooks location
        main_hook = config.repo_root / ".git" / "hooks" / "pre-push"
        if main_hook.exists():
            console.print(f"  [green]✓[/green] Project pre-push hook found")
            console.print(f"  [cyan]ℹ[/cyan] Hooks will be chained in worktrees")
        else:
            console.print(f"  [yellow]![/yellow] No project pre-push hook")
            warnings.append("No project pre-push hook (only orchestrator hook will run)")

    # 5. Check agent commands
    console.print("\n[bold]5. Agent Commands[/bold]")
    for agent_name, agent_config in config.agents.items():
        cmd = agent_config.command
        if cmd:
            # Get first word of command (the executable)
            executable = cmd.split()[0]
            if shutil.which(executable):
                console.print(f"  [green]✓[/green] {agent_name}: {executable} found")
            else:
                console.print(f"  [yellow]![/yellow] {agent_name}: {executable} not in PATH")
                warnings.append(f"Agent '{agent_name}' command '{executable}' not in PATH")
        else:
            console.print(f"  [yellow]![/yellow] {agent_name}: no command configured")
            warnings.append(f"Agent '{agent_name}' has no command")

    # 6. Check tmux (if using tmux mode)
    console.print("\n[bold]6. Tmux[/bold]")
    tmux_path = shutil.which("tmux")
    if tmux_path:
        console.print(f"  [green]✓[/green] tmux found: {tmux_path}")
        # Check version
        version_check = subprocess.run(
            ["tmux", "-V"],
            capture_output=True, text=True
        )
        if version_check.returncode == 0:
            console.print(f"  [cyan]ℹ[/cyan] Version: {version_check.stdout.strip()}")
    else:
        if config.ui_mode == "tmux":
            console.print(f"  [red]✗[/red] tmux not found (required for ui_mode: tmux)")
            errors.append("tmux not installed")
        else:
            console.print(f"  [yellow]![/yellow] tmux not found (ok if using web/none mode)")
            warnings.append("tmux not installed")

    # 7. Verify AI meta-agent hooks
    console.print("\n[bold]7. AI Meta-Agent Hooks[/bold]")
    from .hooks import (
        detect_agents_from_config,
        get_adapter,
        UnsupportedMetaAgentError,
        MetaAgentType,
    )

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    console.print(f"  [cyan]ℹ[/cyan] Detected meta-agents: {[t.value for t in unique_types]}")

    for agent_label, agent_type in agent_types.items():
        console.print(f"    {agent_label} → {agent_type.value}")

    for agent_type in unique_types:
        try:
            adapter = get_adapter(agent_type)

            if adapter.is_installed(config.repo_root):
                console.print(f"  [green]✓[/green] {agent_type.value}: hooks installed")

                # Run thorough verification
                result = adapter.verify_hooks(config.repo_root)

                if result.success:
                    console.print(f"  [green]✓[/green] {agent_type.value}: {len(result.checks_passed)} checks passed")
                    # Show some details on verbose
                    block_checks = [c for c in result.checks_passed if c.startswith("blocks:")]
                    if block_checks:
                        console.print(f"    [dim]Verified blocking: {len(block_checks)} patterns[/dim]")

                    # Live verification if requested
                    if getattr(args, 'live', False):
                        console.print(f"\n  [cyan]🔄[/cyan] Running live verification (spawning Claude)...")
                        timeout = getattr(args, 'live_timeout', 60)
                        live_success, live_msg = adapter.live_verify(config.repo_root, timeout=timeout)
                        if live_success:
                            console.print(f"  [green]✓[/green] Live verification passed")
                            console.print(f"    [dim]{live_msg.split(chr(10))[0]}[/dim]")
                        else:
                            console.print(f"  [red]✗[/red] Live verification failed")
                            console.print(f"    {live_msg}")
                            errors.append(f"{agent_type.value}: live verification failed")
                else:
                    console.print(f"  [red]✗[/red] {agent_type.value}: verification failed")
                    for failure in result.checks_failed:
                        console.print(f"    [red]✗[/red] {failure}")
                        errors.append(f"{agent_type.value}: {failure}")
            else:
                console.print(f"  [yellow]![/yellow] {agent_type.value}: hooks not installed")
                warnings.append(f"{agent_type.value} hooks not installed - run 'issue-orchestrator setup-hooks'")

        except UnsupportedMetaAgentError as e:
            console.print(f"  [red]✗[/red] {agent_type.value}: {e.reason}")
            errors.append(f"Unsupported meta-agent: {e.reason}")

    # Summary
    console.print("\n" + "=" * 50)
    if errors:
        console.print(f"\n[bold red]Verification FAILED: {len(errors)} error(s), {len(warnings)} warning(s)[/bold red]")
        for err in errors:
            console.print(f"  [red]✗[/red] {err}")
        for warn in warnings:
            console.print(f"  [yellow]![/yellow] {warn}")
        return 1
    elif warnings:
        console.print(f"\n[bold yellow]Verification PASSED with {len(warnings)} warning(s)[/bold yellow]")
        for warn in warnings:
            console.print(f"  [yellow]![/yellow] {warn}")
        return 0
    else:
        console.print(f"\n[bold green]Verification PASSED - all checks OK[/bold green]")
        return 0


def cmd_setup_hooks(args: argparse.Namespace) -> int:
    """Install AI meta-agent hooks for the target project."""
    from .hooks import (
        detect_agents_from_config,
        get_adapter,
        UnsupportedMetaAgentError,
    )

    console.print("[bold cyan]Installing AI Meta-Agent Hooks[/bold cyan]\n")

    # Load config
    try:
        config = _load_config(args)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("Create a .issue-orchestrator.yaml config file first.")
        return 1

    # Detect meta-agents from config
    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    console.print(f"[bold]Detected Meta-Agents:[/bold]")
    for agent_label, agent_type in agent_types.items():
        console.print(f"  {agent_label} → {agent_type.value}")

    console.print()

    # Determine target directory
    target_root = Path(args.target).resolve() if hasattr(args, 'target') and args.target else config.repo_root

    console.print(f"[bold]Target Project:[/bold] {target_root}\n")

    errors = []
    installed = []

    for agent_type in unique_types:
        try:
            adapter = get_adapter(agent_type)

            console.print(f"[cyan]Installing hooks for {agent_type.value}...[/cyan]")
            files = adapter.install_hooks(target_root)

            for f in files:
                console.print(f"  [green]✓[/green] {f.relative_to(target_root)}")
                installed.append(f)

            # Verify installation
            result = adapter.verify_hooks(target_root)
            if result.success:
                console.print(f"  [green]✓[/green] Verification passed ({len(result.checks_passed)} checks)")
            else:
                console.print(f"  [yellow]![/yellow] Verification had issues:")
                for failure in result.checks_failed:
                    console.print(f"    [red]✗[/red] {failure}")

        except UnsupportedMetaAgentError as e:
            console.print(f"  [red]✗[/red] {agent_type.value}: {e.reason}")
            errors.append(str(e))

    console.print()

    if errors:
        console.print(f"[bold red]Setup completed with {len(errors)} error(s)[/bold red]")
        console.print("\n[yellow]Some meta-agents are not supported. Consider using Claude Code.[/yellow]")
        return 1

    console.print(f"[bold green]✓ Hooks installed successfully ({len(installed)} files)[/bold green]")
    console.print("\n[dim]Run 'issue-orchestrator verify' to confirm everything is working.[/dim]")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Demonstrate orchestrator features with mock data."""
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from pathlib import Path

    from .control.scheduler import Scheduler
    from .control.dependency_evaluator import DependencyEvaluator
    from .domain.dependencies import parse_dependencies
    from .config import Config
    from .models import Issue, AgentConfig  # Issue used for demo mock creation

    console = Console()

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

    import re
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

    # Create a mock issue checker
    class MockIssueChecker:
        """Mock checker simulating GitHub issue states."""
        def __init__(self):
            # Issue #1 is closed (satisfied), others are open
            self.states = {1: "closed", 2: "open", 3: "open", 4: "open", 5: "open"}

        def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
            return self.states.get(issue_number)

    class CollectingEventSink:
        """Collects events for display."""
        def __init__(self):
            self.events = []
        def publish(self, event):
            self.events.append(event)

    checker = MockIssueChecker()
    events = CollectingEventSink()

    # Create evaluator and scheduler
    evaluator = DependencyEvaluator(issue_checker=checker, events=events)
    config = Config(
        repo="demo/repo",
        repo_root=Path("."),
        agents={"claude": AgentConfig(prompt_path=Path("prompt.txt"), worktree_base=Path("/tmp"))},
        max_concurrent_sessions=2,
    )
    scheduler = Scheduler(config=config, dependency_evaluator=evaluator)

    # Show dependency evaluation
    console.print("[bold]Scenario:[/bold] Issue #1 is CLOSED (completed), issues #2-5 are OPEN")
    console.print()

    console.print("[bold]Dependency Evaluation:[/bold]")
    dep_table = Table(show_header=True, header_style="bold magenta")
    dep_table.add_column("#", style="dim")
    dep_table.add_column("Dependencies")
    dep_table.add_column("Status")
    dep_table.add_column("Runnable?")

    for issue in issues:
        report = evaluator.evaluate(issue.number, issue.body or "")
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
    console.print(f"  Available issues: {len(available)} (would launch up to {config.max_concurrent_sessions})")
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
    console.print(Panel(
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
    ))

    return 0


def main() -> int:
    """Main entry point for the CLI."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Orchestrate AI agents working on GitHub issues"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to config file (default: search for .issue-orchestrator.yaml)"
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
        "--label",
        type=str,
        default=None,
        help="Filter issues by label (e.g., 'agent:test' for e2e testing)"
    )
    start_parser.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Process only this specific issue number"
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
    start_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for web dashboard (default: 8080)"
    )
    start_parser.add_argument(
        "--queue-refresh",
        type=int,
        default=None,
        help="Seconds between queue refreshes from GitHub (default: 600, 0=manual only)"
    )
    start_parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help="Max issues to start processing this session (default: 0=unlimited)"
    )
    start_parser.add_argument(
        "--review-label",
        type=str,
        default=None,
        help="Label to add to PRs for review (e.g., 'needs-triage-review')"
    )
    start_parser.add_argument(
        "--review-threshold",
        type=int,
        default=None,
        help="Auto-trigger triage review after N PRs with review label (default: 0=manual only)"
    )
    start_parser.add_argument(
        "--adopt-sessions",
        action="store_true",
        help="Adopt existing iTerm2 tabs as active sessions (for restart without losing sessions)"
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

    # refresh command
    refresh_parser: argparse.ArgumentParser = subparsers.add_parser(
        "refresh", help="Request immediate refresh of issues from GitHub"
    )
    refresh_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)"
    )
    refresh_parser.set_defaults(func=cmd_refresh)

    # restart command
    restart_parser: argparse.ArgumentParser = subparsers.add_parser(
        "restart", help="Restart orchestrator, preserving existing iTerm2 sessions"
    )
    restart_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)"
    )
    restart_parser.add_argument(
        "--ui-mode",
        choices=["tmux", "iterm2", "web"],
        default=None,
        help="UI mode for new orchestrator"
    )
    restart_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    restart_parser.set_defaults(func=cmd_restart)

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

    # setup command (interactive wizard)
    setup_parser: argparse.ArgumentParser = subparsers.add_parser(
        "setup", help="Interactive setup wizard for new or existing projects"
    )
    setup_parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project directory to set up (default: prompts interactively)"
    )
    setup_parser.set_defaults(func=cmd_setup)

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

    # audit command
    audit_parser: argparse.ArgumentParser = subparsers.add_parser(
        "audit", help="Audit queue - show why issues are queued or skipped"
    )
    audit_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    audit_parser.set_defaults(func=cmd_audit)

    # verify command
    verify_parser: argparse.ArgumentParser = subparsers.add_parser(
        "verify", help="Verify the orchestrator setup works correctly"
    )
    verify_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    verify_parser.add_argument(
        "--live",
        action="store_true",
        help="Perform live verification by spawning Claude and testing hook blocking"
    )
    verify_parser.add_argument(
        "--live-timeout",
        type=int,
        default=60,
        help="Timeout in seconds for live verification (default: 60)"
    )
    verify_parser.set_defaults(func=cmd_verify)

    # setup-hooks command
    setup_hooks_parser: argparse.ArgumentParser = subparsers.add_parser(
        "setup-hooks", help="Install AI meta-agent hooks in target project"
    )
    setup_hooks_parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target project directory (default: repo_root from config)"
    )
    setup_hooks_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    setup_hooks_parser.set_defaults(func=cmd_setup_hooks)

    # demo command
    demo_parser: argparse.ArgumentParser = subparsers.add_parser(
        "demo", help="Demonstrate orchestrator features with mock data"
    )
    demo_parser.set_defaults(func=cmd_demo)

    args: argparse.Namespace = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
