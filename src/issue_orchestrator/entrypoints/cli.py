import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config

from rich.console import Console
from rich.table import Table

from ..infra.logging_config import setup_logging
from .cli_auth_commands import cmd_auth, cmd_keys
from .cli_hook_commands import cmd_harden_repo, cmd_setup_hooks, cmd_verify
from .cli_queue_commands import cmd_audit
from .cli_support import (
    client_dashboard_link as _client_dashboard_link,
    get_repository_host as _get_repository_host,
    load_config as _load_config,
    resolve_repo as _resolve_repo,
    run_test_setup as _run_test_setup,
)
from .cli_utility_commands import cmd_demo, cmd_doctor, cmd_trace

console = Console()
logger = logging.getLogger(__name__)


def _apply_cli_overrides(args: argparse.Namespace, config: "Config") -> None:  # noqa: C901, PLR0912 - one branch per CLI override, inherent to config mapping
    """Apply CLI argument overrides to config."""
    # Handle milestone override
    if hasattr(args, "milestones") and args.milestones:
        milestones = [m.strip() for m in args.milestones.split(",") if m.strip()]
        config.filtering.milestones = milestones
        config.filtering.milestone = None
        console.print(f"[cyan]Filtering by milestones: {', '.join(milestones)}[/cyan]")
    elif hasattr(args, "milestone") and args.milestone:
        config.filtering.milestone = args.milestone
        config.filtering.milestones = []
        console.print(f"[cyan]Filtering by milestone: {args.milestone}[/cyan]")

    # Handle label override
    if hasattr(args, "label") and args.label:
        config.filtering.label = args.label
        console.print(f"[cyan]Filtering by label: {args.label}[/cyan]")

    # Handle single issue filter
    if hasattr(args, "issue") and args.issue:
        config.filtering.issue = args.issue
        console.print(f"[cyan]Processing only issue #{args.issue}[/cyan]")

    # Handle ui_mode override
    if hasattr(args, "ui_mode") and args.ui_mode:
        config.ui_mode = args.ui_mode
    console.print(f"[dim]UI mode: {config.ui_mode}[/dim]")

    # Handle queue_refresh override
    if hasattr(args, "queue_refresh") and args.queue_refresh is not None:
        config.queue_refresh_seconds = args.queue_refresh

    # Handle GH audit overrides
    if hasattr(args, "gh_audit") and args.gh_audit:
        config.gh_audit_enabled = True
    if hasattr(args, "gh_audit_events") and args.gh_audit_events:
        config.gh_audit_events = True
    if hasattr(args, "gh_audit_file") and args.gh_audit_file is not None:
        config.gh_audit_file = args.gh_audit_file

    # Handle max_issues override
    if hasattr(args, "max_issues") and args.max_issues is not None:
        config.filtering.max_to_start = args.max_issues
        if config.filtering.max_to_start > 0:
            console.print(
                f"[dim]Max issues to start: {config.filtering.max_to_start}[/dim]"
            )

    # Handle review workflow overrides
    if hasattr(args, "review_label") and args.review_label is not None:
        config.triage_review_label = args.review_label
        console.print(f"[dim]Review label: {config.triage_review_label}[/dim]")
    if hasattr(args, "review_threshold") and args.review_threshold is not None:
        config.triage_review_threshold = args.review_threshold
        if config.triage_review_threshold > 0:
            console.print(
                f"[dim]Review threshold: {config.triage_review_threshold} PRs[/dim]"
            )


def _run_dry_run(args: argparse.Namespace, config: "Config") -> int:
    """Run dry-run mode - show what would be processed without starting."""
    from ..control.scheduler import Scheduler
    from ..execution.providers import create_repository_host
    from ..infra.analysis import analyze_all_issues, extract_issue_branches
    from ..execution.git_working_copy import GitWorkingCopy

    console.print("\n[cyan]DRY RUN - showing what would be processed:[/cyan]\n")

    scheduler = Scheduler(config)
    github = create_repository_host(config.repo, config=config) if config.repo else None
    working_copy = GitWorkingCopy()
    all_issues = []

    milestones = config.get_filter_milestones()
    if not milestones:
        milestones = [None]

    for agent_label in config.agents.keys():
        labels = [agent_label]
        if config.filtering.label:
            labels.append(config.filtering.label)
        for milestone in milestones:
            if github:
                issues = github.list_issues(
                    labels=labels,
                    milestone=milestone,
                    limit=config.filtering.fetch_limit,
                )
                all_issues.extend(issues)

    if not all_issues:
        console.print("[yellow]No matching issues found.[/yellow]")
        return 0

    # Analyze all issues using shared logic
    issue_branches = extract_issue_branches(
        working_copy.list_remote_branches(config.repo_root)
    )
    states = analyze_all_issues(
        issues=all_issues,
        repo=config.repo,
        issue_branches=issue_branches,
        check_session_fn=lambda _: False,
    )

    # Sort by priority
    states.sort(key=lambda s: s.issue.priority)

    _print_dry_run_table(states)
    _print_dry_run_summary(states, all_issues, scheduler, config)
    _print_orphan_branches(states, config, github, working_copy)

    return 0


def _print_dry_run_table(states: list) -> None:
    """Print the issues table for dry-run mode."""
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
        session_status = (
            "[green]active[/green]" if state.has_session else "[dim]none[/dim]"
        )
        branch_status = (
            f"[cyan]{state.branch[:20]}...[/cyan]"
            if state.branch and len(state.branch) > 20
            else f"[cyan]{state.branch}[/cyan]"
            if state.branch
            else "[dim]none[/dim]"
        )

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


def _print_dry_run_summary(
    states: list, all_issues: list, scheduler, config: "Config"
) -> None:
    """Print summary statistics for dry-run mode."""
    available, _ = scheduler.get_available_issues(all_issues, check_dependencies=False)
    console.print(f"\n[dim]Total issues: {len(all_issues)}[/dim]")
    console.print(f"[dim]Available to process: {len(available)}[/dim]")
    console.print(
        f"[dim]Would launch up to {config.max_concurrent_sessions} concurrent sessions[/dim]"
    )

    # Warnings for stale issues
    stale_states = [s for s in states if s.is_stale]
    if stale_states:
        console.print(
            f"\n[yellow]Warning: {len(stale_states)} issue(s) marked in-progress but have no active session:[/yellow]"
        )
        for state in stale_states:
            if state.branch:
                console.print(
                    f"  [yellow]#{state.issue.number}[/yellow]: {state.issue.title[:35]} [cyan](has branch: {state.branch})[/cyan]"
                )
            else:
                console.print(
                    f"  [yellow]#{state.issue.number}[/yellow]: {state.issue.title[:40]}"
                )
        console.print("\n[dim]Options:[/dim]")
        console.print(
            "[dim]  - Reset to restart fresh: gh issue edit # --remove-label in-progress[/dim]"
        )
        console.print(
            "[dim]  - Resume from branch: orchestrator will checkout existing branch if present[/dim]"
        )


def _print_orphan_branches(
    states: list, config: "Config", github, working_copy
) -> None:
    """Print orphan branches analysis for dry-run mode."""
    from ..infra.analysis import extract_issue_branches, analyze_orphan_branches

    issue_branches = extract_issue_branches(
        working_copy.list_remote_branches(config.repo_root)
    )
    in_progress_nums = {s.issue.number for s in states if s.issue.is_in_progress}
    orphan_states = analyze_orphan_branches(
        issue_branches,
        in_progress_nums,
        config.repo,
        issue_tracker=github,
        pr_tracker=github,
        commits_ahead_fn=lambda b: working_copy.get_commits_ahead_count(
            config.repo_root, b
        ),
        last_commit_date_fn=lambda b: working_copy.get_last_commit_date(
            config.repo_root, b
        ),
    )

    if not orphan_states:
        return

    console.print(
        f"\n[yellow]Warning: {len(orphan_states)} orphan branch(es) found:[/yellow]"
    )

    orphan_table = Table(title=None, box=None)
    orphan_table.add_column("#", style="cyan", width=6)
    orphan_table.add_column("Branch", style="dim")
    orphan_table.add_column("Issue", style="white")
    orphan_table.add_column("Commits", style="magenta", width=7)
    orphan_table.add_column("Age", style="dim", width=12)
    orphan_table.add_column("Action", style="yellow")

    for orphan in orphan_states:
        issue_info = _format_orphan_issue_info(orphan)
        action = _format_orphan_action(orphan)
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
    delete_count = sum(
        1 for o in orphan_states if o.suggested_action == "delete-branch"
    )
    if resume_count > 0:
        console.print(
            f"\n[dim]To resume work on open issues, add in-progress label:[/dim]"
        )
        from ..control.label_manager import LabelManager

        _lm = LabelManager(config)
        console.print(f"[dim]  gh issue edit # --add-label {_lm.in_progress}[/dim]")
    if delete_count > 0:
        console.print(f"\n[dim]To clean up stale branches:[/dim]")
        console.print(f"[dim]  git push origin --delete <branch-name>[/dim]")


def _format_orphan_issue_info(orphan) -> str:
    """Format issue info for orphan branch display."""
    if orphan.issue_title:
        title_short = orphan.issue_title[:25] + (
            "..." if len(orphan.issue_title) > 25 else ""
        )
        state_color = "green" if orphan.issue_state == "open" else "red"
        return f"[{state_color}]{orphan.issue_state}[/{state_color}]: {title_short}"
    elif orphan.issue_state:
        state_color = "green" if orphan.issue_state == "open" else "red"
        return f"[{state_color}]{orphan.issue_state}[/{state_color}]"
    return "[dim]not found[/dim]"


def _format_orphan_action(orphan) -> str:
    """Format suggested action for orphan branch display."""
    action_styles = {
        "resume-work": "[green]resume[/green]",
        "investigate": "[yellow]investigate[/yellow]",
        "delete-branch": "[red]delete[/red]",
    }
    return action_styles.get(orphan.suggested_action) or str(orphan.suggested_action)


async def _run_no_dashboard(orchestrator, api_port: int | None) -> None:
    """Run orchestrator without dashboard UI."""
    from .control_api import ControlAPIServer

    control_api = None
    if api_port is not None:
        control_api = ControlAPIServer(orchestrator, port=api_port)
        try:
            await control_api.start()
        except OSError as exc:
            logging.warning("Control API failed to start on port %s: %s", api_port, exc)
            control_api = None

    try:
        await orchestrator.startup()
        await orchestrator.run_loop()
    finally:
        if control_api:
            await control_api.stop()


async def _run_web_dashboard(
    orchestrator, config: "Config", args: argparse.Namespace, api_port: int | None
) -> None:
    """Run orchestrator with web dashboard."""
    import signal
    from .web import run_with_web_dashboard, trigger_server_shutdown
    from .control_api import ControlAPIServer

    def handle_signal():
        if orchestrator.shutdown_requested:
            orchestrator.request_shutdown(force=True)
            trigger_server_shutdown()
        else:
            orchestrator.request_shutdown()
            trigger_server_shutdown()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, handle_signal)
    loop.add_signal_handler(signal.SIGTERM, handle_signal)

    control_api = None
    if api_port is not None:
        if api_port != 0:
            console.print(f"[dim]Control API on http://127.0.0.1:{api_port}[/dim]")
        control_api = ControlAPIServer(orchestrator, port=api_port)
        try:
            await control_api.start()
            if api_port == 0:
                console.print(
                    f"[dim]Control API on http://127.0.0.1:{control_api.port}[/dim]"
                )
        except OSError as exc:
            logging.warning("Control API failed to start on port %s: %s", api_port, exc)
            control_api = None

    try:
        port = args.port if args.port != 8080 else config.web_port
        await run_with_web_dashboard(orchestrator, port=port)
    finally:
        if control_api:
            await control_api.stop()


async def _run_tui_dashboard(
    orchestrator, config: "Config", api_port: int | None
) -> bool:
    """Run orchestrator with TUI dashboard."""
    from .control_api import ControlAPIServer
    from .dashboard import run_with_dashboard

    control_api = None
    if api_port is not None:
        control_api = ControlAPIServer(orchestrator, port=api_port)
        await control_api.start()

    try:
        await orchestrator.startup()
        return await run_with_dashboard(orchestrator, config.ui_mode)
    finally:
        if control_api:
            await control_api.stop()


def cmd_start(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912 - CLI entry point with config/logging/validation/startup phases
    """Start the orchestrator."""
    debug = getattr(args, "debug", False)
    no_dashboard = getattr(args, "no_dashboard", False)
    log_level = "DEBUG" if debug else "INFO"

    console.print("[green]Starting issue-orchestrator...[/green]")

    try:
        from .bootstrap import build_orchestrator
        from ..infra.repo_lock import is_locked, read_lock
        from ..infra import supervisor

        # Load config first - repo_root is derived from config file location
        config = _load_config(args)

        # Set up logging to repo-scoped log file
        log_file = setup_logging(
            repo_root=config.repo_root,
            level=log_level,
            console_output=no_dashboard,
            log_retention_days=config.log_retention_days,
        )

        if debug and log_file:
            console.print(f"[dim]Debug logging enabled (tail -f {log_file})[/dim]")

        # Validate configuration early - fail fast with clear errors
        validation_errors = config.validate()
        if validation_errors:
            console.print("[red]Configuration errors:[/red]")
            for error in validation_errors:
                console.print(f"  [red]• {error}[/red]")
                logging.error(f"Config validation: {error}")
            return 1

        # Run doctor checks including guardrails - fail fast if environment is broken
        from ..infra.launcher import launch_preflight_only
        from ..execution.command_runner import LocalCommandRunner

        launch_result = launch_preflight_only(
            config=config, runner=LocalCommandRunner()
        )
        if launch_result.status == "doctor_error":
            console.print("[red]Startup checks failed:[/red]")
            for check in launch_result.doctor.checks:
                if check.status == "error":
                    console.print(f"  [red]✗ {check.name}: {check.detail}[/red]")
                    logging.error(f"Doctor check failed: {check.name}: {check.detail}")
            console.print(
                "\n[yellow]Run 'issue-orchestrator doctor' for full diagnostics[/yellow]"
            )
            return 1
        elif launch_result.status == "doctor_warning":
            for check in launch_result.doctor.checks:
                if check.status == "warning":
                    console.print(f"  [yellow]⚠ {check.name}: {check.detail}[/yellow]")

        logger.info(
            "Effective config: repo=%s config_path=%s filter_label=%s ui_mode=%s web_port=%s api_port=%s "
            "max_sessions=%s session_timeout=%s queue_refresh=%s "
            "gh_write_verify_timeout=%s gh_write_verify_initial_ms=%s gh_write_verify_max_ms=%s "
            "gh_write_verify_backoff=%s gh_write_verify_jitter_ms=%s "
            "gh_audit=%s gh_audit_events=%s gh_audit_file=%s",
            config.repo,
            config.config_path,
            config.filtering.label,
            config.ui_mode,
            config.web_port,
            config.control_api_port,
            config.max_concurrent_sessions,
            config.session_timeout_minutes,
            config.queue_refresh_seconds,
            config.gh_write_verify_timeout_seconds,
            config.gh_write_verify_initial_delay_ms,
            config.gh_write_verify_max_delay_ms,
            config.gh_write_verify_backoff,
            config.gh_write_verify_jitter_ms,
            config.gh_audit_enabled,
            config.gh_audit_events,
            config.gh_audit_file,
        )
        logger.debug(
            "CLI args: label=%s milestone=%s milestones=%s issue=%s no_dashboard=%s ui_mode=%s port=%s api_port=%s test_mode=%s",
            getattr(args, "label", None),
            getattr(args, "milestone", None),
            getattr(args, "milestones", None),
            getattr(args, "issue", None),
            getattr(args, "no_dashboard", None),
            getattr(args, "ui_mode", None),
            getattr(args, "port", None),
            getattr(args, "api_port", None),
            getattr(args, "test_mode", None),
        )
        override_pairs = getattr(args, "set", None) or []
        if override_pairs:
            logger.debug("CLI overrides: %s", override_pairs)
        logger.debug("Config worktree_base=%s", config.worktree_base)
        for label, agent in config.agents.items():
            logger.debug(
                "Agent config: label=%s prompt=%s model=%s timeout=%s command=%s permission_mode=%s",
                label,
                agent.prompt_path,
                agent.model,
                agent.timeout_minutes,
                agent.command,
                agent.permission_mode,
            )

    except FileNotFoundError as e:
        logging.error(f"Config not found: {e}")
        console.print(f"[red]Error: {e}[/red]")
        console.print("No config found. Run 'issue-orchestrator setup' to create one.")
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
        _run_test_setup(config)
        config.filtering.label = "test-data"
        console.print("[cyan]Test mode: filtering.label set to 'test-data'[/cyan]")

    # Apply CLI argument overrides to config
    _apply_cli_overrides(args, config)

    console.print(f"[dim]Loaded config with {len(config.agents)} agent types[/dim]")
    console.print(
        f"[dim]Max concurrent sessions: {config.max_concurrent_sessions}[/dim]"
    )

    # Handle dry-run mode
    if hasattr(args, "dry_run") and args.dry_run:
        return _run_dry_run(args, config)

    if is_locked(config.repo_root):
        info = read_lock(config.repo_root)
        if info:
            console.print(
                f"[yellow]Orchestrator already running (pid={info.pid}, port={info.http_port}).[/yellow]"
            )
        if sys.stdin.isatty():
            choice = console.input("Abort start? [Y/n]: ").strip().lower() or "y"
            if choice in {"y", "yes"}:
                return 1
            console.print("[yellow]Stopping existing orchestrator...[/yellow]")
            stopped = supervisor.stop(config.repo_root, force=True)
            if not stopped:
                console.print("[red]Failed to stop existing orchestrator.[/red]")
                return 1
        else:
            console.print(
                "[red]Non-interactive start aborted (orchestrator already running).[/red]"
            )
            return 1

    orchestrator = build_orchestrator(config=config)

    # Get control API port (CLI --api-port overrides config; 0 = auto-assign)
    cli_api_port = getattr(args, "api_port", None)
    api_port = cli_api_port if cli_api_port is not None else config.control_api_port

    try:
        if args.no_dashboard:
            # Run orchestrator without dashboard (useful for CI/debugging)
            console.print("[dim]Running without dashboard UI[/dim]")
            if api_port and api_port != 0:
                console.print(f"[dim]Control API on http://127.0.0.1:{api_port}[/dim]")
            asyncio.run(_run_no_dashboard(orchestrator, api_port))
        elif config.ui_mode == "web":
            # Run with web dashboard in browser
            port = args.port if args.port != 8080 else config.web_port
            console.print("[dim]Starting web dashboard...[/dim]")
            if port != 0:
                console.print(
                    f"[green]Dashboard will open at {_client_dashboard_link(port)}[/green]"
                )
            asyncio.run(_run_web_dashboard(orchestrator, config, args, api_port))
        else:
            # Run with interactive TUI dashboard (tmux mode)
            if api_port and api_port != 0:
                console.print(f"[dim]Control API on http://127.0.0.1:{api_port}[/dim]")
            asyncio.run(_run_tui_dashboard(orchestrator, config, api_port))
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show current status."""
    try:
        config = _load_config(args)

        console.print("\n[cyan]Orchestrator Status[/cyan]")
        console.print(f"\n[bold]Config:[/bold]")
        console.print(f"  Repo: {config.repo or '(auto-detect)'}")
        console.print(f"  Max sessions: {config.max_concurrent_sessions}")
        console.print(f"  Agents: {', '.join(config.agents.keys())}")
        if config.filtering.label:
            console.print(f"  Filter label: {config.filtering.label}")
        if config.filtering.milestones:
            console.print(
                f"  Filter milestones: {', '.join(config.filtering.milestones)}"
            )
        elif config.filtering.milestone:
            console.print(f"  Filter milestone: {config.filtering.milestone}")

        console.print(
            "\n[dim]Note: Use the web dashboard to view active sessions[/dim]"
        )
        return 0
    except FileNotFoundError:
        console.print("[yellow]Orchestrator not configured yet[/yellow]")
        return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach to session (deprecated - use web dashboard)."""
    console.print("[yellow]The 'attach' command is no longer available.[/yellow]")
    console.print("Use the web dashboard to view sessions: issue-orchestrator start")
    return 1


def cmd_switch(args: argparse.Namespace) -> int:
    """Switch to session (deprecated - use web dashboard)."""
    console.print("[yellow]The 'switch' command is no longer available.[/yellow]")
    console.print("Use the web dashboard to view sessions: issue-orchestrator start")
    return 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Switch to dashboard (deprecated - use web dashboard)."""
    console.print("[yellow]The 'dashboard' command is no longer available.[/yellow]")
    console.print(
        "Start the orchestrator to access the web dashboard: issue-orchestrator start"
    )
    return 1


def cmd_output(args: argparse.Namespace) -> int:
    """Show recent output from an issue's session."""
    console.print("[yellow]The 'output' command is no longer available.[/yellow]")
    console.print(
        "View session recordings in .issue-orchestrator/sessions/<run>/terminal-recording.jsonl"
    )
    return 1


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
            console.print(
                "[green]Refresh requested - issues will be fetched on next loop iteration[/green]"
            )
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
    """Restart the orchestrator.

    This command:
    1. Sends shutdown to the running orchestrator (via API)
    2. Waits for it to exit
    3. Starts a new orchestrator
    """
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
            console.print(
                f"[yellow]Orchestrator responded with {resp.status_code}[/yellow]"
            )
    except httpx.ConnectError:
        console.print(f"[yellow]No orchestrator running on port {port}[/yellow]")
        console.print("[cyan]Starting fresh...[/cyan]")
        # Just start fresh
        return _start_fresh(args)

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
                console.print(f"[dim]Still waiting... ({i + 1}s)[/dim]")
        except httpx.ConnectError:
            # Orchestrator has exited
            console.print("[green]Orchestrator stopped[/green]")
            break
    else:
        console.print(
            "[yellow]Orchestrator didn't stop in time, continuing anyway...[/yellow]"
        )

    # Step 4: Start new orchestrator
    console.print("[cyan]Starting new orchestrator...[/cyan]")
    return _start_fresh(args)


def _start_fresh(args: argparse.Namespace) -> int:
    """Start a fresh orchestrator instance."""
    import sys

    # Build command to run start
    cmd = [sys.executable, "-m", "issue_orchestrator.entrypoints.cli", "start"]

    # Pass through relevant flags
    if hasattr(args, "config") and args.config:
        cmd.extend(["--config", args.config])
    if hasattr(args, "port") and args.port:
        cmd.extend(["--port", str(args.port)])
    if hasattr(args, "debug") and args.debug:
        cmd.append("--debug")
    if hasattr(args, "ui_mode") and args.ui_mode:
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
    console.print(f"[cyan]Prioritizing issue #{issue_number}...[/cyan]")
    # TODO: implement prioritization logic
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the interactive setup wizard."""
    from pathlib import Path

    from .cli_tools.setup_wizard import run_wizard

    target_path = Path(args.path).expanduser().resolve() if args.path else None
    dry_run = getattr(args, "dry_run", False)
    run_wizard(target_path, dry_run=dry_run)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize required GitHub labels."""
    try:
        config = _load_config(args)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("No config found. Run 'issue-orchestrator setup' to create one.")
        return 1

    try:
        repo = _resolve_repo(config)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1
    if not repo:
        console.print("[red]Error: repo must be set in config[/red]")
        return 1

    console.print(f"[cyan]Initializing labels for {repo}...[/cyan]\n")
    client = _get_repository_host(config)
    if client is None:
        console.print("[red]Error: Unable to create GitHub client[/red]")
        return 1

    # Collect all labels to create
    from ..control.label_manager import LabelManager

    _lm = LabelManager(config)
    labels = [
        _lm.in_progress,
        _lm.blocked,
        _lm.needs_human,
        "priority:high",
        "priority:medium",
        "priority:low",
    ]
    # Add all agent labels from config
    labels.extend(config.agents.keys())

    created = 0
    updated = 0
    failed = 0
    existing = {
        label.get("name") for label in client.list_labels() if isinstance(label, dict)
    }

    for label in labels:
        try:
            client.create_label(label, force=True)
            if label in existing:
                console.print(f"  [yellow]↻[/yellow] {label}")
                updated += 1
            else:
                console.print(f"  [green]✓[/green] {label}")
                created += 1
        except Exception as exc:
            console.print(f"  [red]✗[/red] {label}: {exc}")
            failed += 1

    # Print summary
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Created: {created}")
    console.print(f"  Updated: {updated}")
    console.print(f"  Failed: {failed}")

    if failed > 0:
        console.print(
            "\n[yellow]Some labels failed to create. Check your GitHub token/auth.[/yellow]"
        )
        return 1

    console.print("\n[green]✓ Label initialization complete![/green]")
    return 0


def cmd_test_reset(args: argparse.Namespace) -> int:
    """Reset test environment: teardown + setup."""
    import subprocess
    import sys
    from pathlib import Path

    console.print("[bold]Test Reset: Clean slate for integration testing[/bold]\n")

    # Find the scripts directory (4 levels up from entrypoints/cli.py)
    scripts_dir = Path(__file__).parent.parent.parent.parent / "scripts"
    if not scripts_dir.exists():
        # Try installed package location
        scripts_dir = Path(__file__).parent.parent / "scripts"

    if not scripts_dir.exists():
        console.print("[red]Error: scripts directory not found[/red]")
        return 1

    # Step 1: Teardown
    console.print("[cyan]Step 1: Tearing down existing test data...[/cyan]")
    teardown_script = scripts_dir / "teardown_test_issues.py"
    if teardown_script.exists():
        result = subprocess.run([sys.executable, str(teardown_script)])
        if result.returncode != 0:
            console.print(
                "[yellow]Warning: Teardown had issues, continuing...[/yellow]"
            )
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


def cmd_e2e_reset(args: argparse.Namespace) -> int:
    """Reset E2E run history: delete all runs, test results, and artifacts."""
    from ..infra.e2e_db import E2EDB

    config = _load_config(args)
    repo_root = config.repo_root
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"

    if not db_path.exists():
        console.print("[yellow]No E2E database found — nothing to reset.[/yellow]")
        return 0

    # Load timeline store for timeline event cleanup
    timeline_store = None
    timeline_db_path = repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    if timeline_db_path.exists():
        from ..execution.timeline_store import SqliteTimelineStore

        timeline_store = SqliteTimelineStore(db_path=timeline_db_path)

    db = E2EDB(db_path)
    counts = db.reset_all_history(timeline_store=timeline_store)

    console.print("[bold]E2E history reset complete:[/bold]")
    for table, count in counts.items():
        console.print(f"  {table}: {count} deleted")

    # Also clean up log directory
    log_dir = repo_root / ".issue-orchestrator" / "logs" / "e2e"
    if log_dir.is_dir():
        import shutil

        shutil.rmtree(log_dir, ignore_errors=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        console.print("  log files: directory cleared")

    console.print("\n[green]Done. E2E history is now empty.[/green]")
    return 0


def cmd_default(args: argparse.Namespace) -> int:  # noqa: ARG001 - args unused but required for command signature
    """Default command when no subcommand is given - open unified dashboard."""
    import webbrowser

    from ..observation.instance_detector import (
        detect_system_state,
        get_best_entry_point,
    )

    console.print("[cyan]Issue Orchestrator[/cyan]")

    # Detect current state
    state = detect_system_state()
    entry = get_best_entry_point(state)

    if entry["action"] == "open_dashboard":
        # Dashboard is already running, just open it
        console.print(f"[dim]Dashboard already running on port {entry['port']}[/dim]")
        console.print(f"[green]Opening {entry['url']}[/green]")
        webbrowser.open(entry["url"])
        return 0

    else:
        # Need to start the dashboard
        console.print("[dim]Starting dashboard...[/dim]")

        # Start the control center (dashboard) server
        import subprocess
        import sys
        import time

        port = entry["port"]
        repo_path = entry.get("repo_path")
        url = _client_dashboard_link(port, repo_path=repo_path)

        # Start control center as a subprocess
        cmd = [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.control_center",
            "--port",
            str(port),
            "--no-browser",  # We'll open browser ourselves
        ]

        # Start in background, but capture stderr for error reporting
        import tempfile

        stderr_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".log")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            start_new_session=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_CC_REPO_ROOT": str(Path.cwd().resolve()),
            },
        )

        # Health check: verify server is actually responding
        import urllib.request
        import urllib.error

        health_url = f"http://localhost:{port}/control/info"
        max_attempts = 10
        for _ in range(max_attempts):
            time.sleep(0.5)

            # Check if process died
            if process.poll() is not None:
                stderr_file.close()
                with open(stderr_file.name) as f:
                    error_output = f.read()
                console.print("[red]Failed to start dashboard server[/red]")
                if "address already in use" in error_output.lower():
                    console.print(
                        f"[yellow]Port {port} is already in use. Kill the existing process:[/yellow]"
                    )
                    console.print(f"  lsof -ti :{port} | xargs kill")
                elif error_output.strip():
                    console.print(f"[dim]Error: {error_output[:500]}[/dim]")
                return 1

            # Try to reach the server
            try:
                urllib.request.urlopen(health_url, timeout=1)
                # Success!
                stderr_file.close()
                console.print(f"[green]Dashboard started on {url}[/green]")
                webbrowser.open(url)
                return 0
            except urllib.error.URLError:
                continue  # Not ready yet

        # Timed out waiting for server
        console.print("[red]Dashboard server failed to respond[/red]")
        console.print(f"[dim]Check logs or try: curl {health_url}[/dim]")
        return 1


def main() -> int:
    """Main entry point for the CLI."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Orchestrate AI agents working on GitHub issues"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to config file (default: .issue-orchestrator/config/default.yaml)",
    )
    parser.add_argument(
        "--set",
        action="append",
        help="Override config value (path=value). Use YAML/JSON for lists or dicts.",
    )
    subparsers: Any = parser.add_subparsers(dest="command", required=False)

    # start command
    start_parser: argparse.ArgumentParser = subparsers.add_parser(
        "start", help="Start the orchestrator"
    )
    start_parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run without dashboard UI (useful for CI/debugging)",
    )
    start_parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Clear test issues, create fresh ones, and run with filter_label=test-data",
    )
    start_parser.add_argument(
        "--milestone", type=str, default=None, help="Filter issues by milestone name"
    )
    start_parser.add_argument(
        "--milestones",
        type=str,
        default=None,
        help="Filter issues by milestone names (comma-separated)",
    )
    start_parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="Filter issues by label (e.g., 'agent:test' for e2e testing)",
    )
    start_parser.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Process only this specific issue number",
    )
    start_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what issues would be processed without launching sessions",
    )
    start_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose DEBUG-level logging to ~/.issue-orchestrator.log",
    )
    start_parser.add_argument(
        "--ui-mode",
        choices=["web"],
        default=None,
        help="UI mode: web (browser dashboard, default)",
    )
    start_parser.add_argument(
        "--port", type=int, default=8080, help="Port for web dashboard (default: 8080)"
    )
    start_parser.add_argument(
        "--api-port",
        type=int,
        default=None,
        dest="api_port",
        help="Port for control API (default: 19080, 0=disabled). Control API is always available regardless of UI mode.",
    )
    start_parser.add_argument(
        "--queue-refresh",
        type=int,
        default=None,
        help="Seconds between queue refreshes from GitHub (default: 600, 0=manual only)",
    )
    start_parser.add_argument(
        "--gh-audit",
        action="store_true",
        help="Enable GH audit reporting (overrides config)",
    )
    start_parser.add_argument(
        "--gh-audit-events",
        action="store_true",
        help="Emit GH audit events to the event stream (overrides config)",
    )
    start_parser.add_argument(
        "--gh-audit-file",
        type=str,
        default=None,
        help="Path for GH audit report output (supports {pid})",
    )
    start_parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help="Max issues to start processing this session (default: 0=unlimited)",
    )
    start_parser.add_argument(
        "--review-label",
        type=str,
        default=None,
        help="Label to add to PRs for review (e.g., 'needs-triage-review')",
    )
    start_parser.add_argument(
        "--review-threshold",
        type=int,
        default=None,
        help="Auto-trigger triage review after N PRs with review label (default: 0=manual only)",
    )
    start_parser.set_defaults(func=cmd_start)

    # status command
    status_parser: argparse.ArgumentParser = subparsers.add_parser(
        "status", help="Show current status"
    )
    status_parser.set_defaults(func=cmd_status)

    # attach command (deprecated)
    attach_parser: argparse.ArgumentParser = subparsers.add_parser(
        "attach", help="(deprecated) Use web dashboard instead"
    )
    attach_parser.add_argument(
        "issue_number",
        type=int,
        nargs="?",
        default=None,
        help="Optional: switch to this issue's window after attaching",
    )
    attach_parser.set_defaults(func=cmd_attach)

    # switch command (deprecated)
    switch_parser: argparse.ArgumentParser = subparsers.add_parser(
        "switch", help="(deprecated) Use web dashboard instead"
    )
    switch_parser.add_argument(
        "issue_number", type=int, help="GitHub issue number to switch to"
    )
    switch_parser.set_defaults(func=cmd_switch)

    # dashboard command (deprecated)
    dashboard_parser: argparse.ArgumentParser = subparsers.add_parser(
        "dashboard", help="(deprecated) Use web dashboard instead"
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    # output command
    output_parser: argparse.ArgumentParser = subparsers.add_parser(
        "output", help="Show recent output from an issue's session"
    )
    output_parser.add_argument("issue_number", type=int, help="GitHub issue number")
    output_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=20,
        help="Number of lines to show (default: 20)",
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
        help="Port of running orchestrator (default: 8080)",
    )
    refresh_parser.set_defaults(func=cmd_refresh)

    # restart command
    restart_parser: argparse.ArgumentParser = subparsers.add_parser(
        "restart", help="Restart the orchestrator"
    )
    restart_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)",
    )
    restart_parser.add_argument(
        "--ui-mode", choices=["web"], default=None, help="UI mode for new orchestrator"
    )
    restart_parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging"
    )
    restart_parser.set_defaults(func=cmd_restart)

    # next command
    next_parser: argparse.ArgumentParser = subparsers.add_parser(
        "next", help="Prioritize an issue"
    )
    next_parser.add_argument(
        "issue_number", type=int, help="GitHub issue number to prioritize"
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
        help="Project directory to set up (default: prompts interactively)",
    )
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what files would be created/modified without writing them",
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

    # e2e-reset command
    e2e_reset_parser: argparse.ArgumentParser = subparsers.add_parser(
        "e2e-reset",
        help="Clear all E2E run history (runs, results, logs, timeline events)",
    )
    e2e_reset_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    e2e_reset_parser.set_defaults(func=cmd_e2e_reset)

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
        "--test-ai-gate",
        action="store_true",
        help="Test AI gating (hooks/execpolicy) for configured agents",
    )
    verify_parser.add_argument(
        "--ai-gate-timeout",
        type=int,
        default=60,
        help="Timeout in seconds for AI gate tests (default: 60)",
    )
    verify_parser.set_defaults(func=cmd_verify)

    # setup-hooks command
    setup_hooks_parser: argparse.ArgumentParser = subparsers.add_parser(
        "setup-hooks", help="Install AI agent hooks in target project"
    )
    setup_hooks_parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target project directory (default: repo_root from config)",
    )
    setup_hooks_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    setup_hooks_parser.set_defaults(func=cmd_setup_hooks)

    harden_repo_parser: argparse.ArgumentParser = subparsers.add_parser(
        "harden-repo",
        help="Install repo-local pre-push guardrails and AI agent hooks",
    )
    harden_repo_parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target project directory (default: repo_root from config)",
    )
    harden_repo_parser.add_argument(
        "--hooks-dir",
        type=str,
        default=None,
        help="Repo-local hooks directory to use for core.hooksPath (default: existing value or .githooks)",
    )
    harden_repo_parser.add_argument(
        "--validation-cmd",
        type=str,
        default=None,
        help="Override validation.cmd when generating scripts/verify-pr.sh",
    )
    harden_repo_parser.add_argument(
        "--config", type=Path, help="Path to config file (default: auto-detect)"
    )
    harden_repo_parser.set_defaults(func=cmd_harden_repo)

    # auth command
    auth_parser: argparse.ArgumentParser = subparsers.add_parser(
        "auth", help="Manage GitHub authentication"
    )
    auth_subparsers = auth_parser.add_subparsers(dest="auth_action")

    auth_store_parser = auth_subparsers.add_parser(
        "store", help="Store GitHub token in OS keychain"
    )
    auth_store_parser.add_argument(
        "--token", "-t", type=str, help="GitHub token (will prompt if not provided)"
    )

    auth_subparsers.add_parser("clear", help="Clear GitHub token from OS keychain")

    auth_parser.set_defaults(func=cmd_auth)

    # keys command (AI provider API keys)
    keys_parser: argparse.ArgumentParser = subparsers.add_parser(
        "keys", help="Manage AI provider API keys"
    )
    keys_subparsers = keys_parser.add_subparsers(dest="keys_action")

    keys_subparsers.add_parser("list", help="List stored API keys")

    keys_set_parser = keys_subparsers.add_parser(
        "set", help="Store an API key in keyring"
    )
    keys_set_parser.add_argument(
        "key_name", help="Key name (e.g., ANTHROPIC_API_KEY or just 'anthropic')"
    )

    keys_delete_parser = keys_subparsers.add_parser(
        "delete", help="Remove an API key from keyring"
    )
    keys_delete_parser.add_argument("key_name", help="Key name to remove")

    keys_parser.set_defaults(func=cmd_keys)

    # doctor command (unified diagnostics)
    doctor_parser: argparse.ArgumentParser = subparsers.add_parser(
        "doctor", help="Run diagnostics on configuration and environment"
    )
    doctor_parser.add_argument("--config", "-c", type=str, help="Path to config file")
    doctor_parser.set_defaults(func=cmd_doctor)

    # demo command
    demo_parser: argparse.ArgumentParser = subparsers.add_parser(
        "demo", help="Demonstrate orchestrator features with mock data"
    )
    demo_parser.set_defaults(func=cmd_demo)

    # trace command
    trace_parser: argparse.ArgumentParser = subparsers.add_parser(
        "trace", help="Trace log entries for a specific issue"
    )
    trace_parser.add_argument("issue_number", type=int, help="Issue number to trace")
    trace_parser.set_defaults(func=cmd_trace)

    args: argparse.Namespace = parser.parse_args()

    # If no command specified, run the default (open dashboard)
    if args.command is None:
        return cmd_default(args)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
