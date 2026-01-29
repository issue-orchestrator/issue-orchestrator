import argparse
import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports import RepositoryHost

from rich.console import Console
from rich.table import Table

from ..infra.logging_config import setup_logging

console = Console()
logger = logging.getLogger(__name__)


def _resolve_repo(config: "Config") -> str:
    from ..execution.providers import get_repo_from_git

    repo = config.repo or get_repo_from_git()
    if repo is None:
        raise ValueError("Could not determine repository. Set 'repo' in config or run from a git directory.")
    return repo


def _get_repository_host(config: "Config") -> "RepositoryHost | None":
    """Get a RepositoryHost for the given config.

    All GitHub access in CLI is routed through the repository host for
    consistent auditing and rate-limit handling.
    """
    from ..execution.providers import create_repository_host

    try:
        repo = _resolve_repo(config)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return None
    if not repo:
        console.print("[red]Error: repo must be set in config[/red]")
        return None
    return create_repository_host(repo=repo)


def _build_action_applier(config: "Config", adapter: "RepositoryHost"):
    from ..control.action_applier import ActionApplier
    from ..control.session_manager import SessionManager
    from ..ports import NullEventSink, NullSessionRunner

    events = NullEventSink()
    sessions = SessionManager(runner=NullSessionRunner(), events=events, config=config)
    return ActionApplier(
        labels=adapter,
        sessions=sessions,
        events=events,
        repository_host=adapter,
    )


def _run_test_setup(config: "Config") -> bool:  # noqa: C901 - inherent complexity from multi-step setup with graceful error handling
    """Run test teardown and setup. Returns True on success."""
    adapter = _get_repository_host(config)
    if adapter is None:
        return False
    action_applier = _build_action_applier(config, adapter)
    try:
        repo = _resolve_repo(config)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return False

    console.print("[cyan]Test mode: Cleaning up old test issues...[/cyan]")

    try:
        from ..control.actions import AddCommentAction

        # Adapter returns list[Issue] with .number attribute
        issues = adapter.list_issues(labels=["test-data"], state="open", limit=100)
        for issue in issues:
            result = action_applier.apply(
                AddCommentAction(
                    number=issue.number,
                    comment="Closed by test mode startup.",
                    reason="test mode cleanup",
                )
            )
            if not result.success:
                logger.warning(
                    "Failed to add test cleanup comment for #%d: %s",
                    issue.number,
                    result.error or "unknown error",
                )
            adapter.update_issue_state(issue.number, "closed")
            console.print(f"  Closed #{issue.number}")
    except Exception as exc:
        logger.warning("Test setup cleanup failed: %s", exc)

    console.print("[cyan]Test mode: Creating fresh test issues...[/cyan]")

    # Create test-data label if missing
    try:
        adapter.create_label(
            "test-data",
            description="Test data for integration tests",
            force=True,
        )
    except Exception as exc:
        logger.warning("Failed to ensure test-data label: %s", exc)

    # Create 5 test issues (matches scripts/setup_test_issues.py)
    test_issues = [
        ("[TEST] Simple backend task", "agent:backend", "priority:high"),
        ("[TEST] Frontend feature", "agent:frontend", "priority:medium"),
        ("[TEST] Mobile bug fix", "agent:mobile", "priority:low"),
        ("[TEST] Task that will block", "agent:backend", None),
        ("[TEST] Task with dependency", "agent:backend", None),
    ]

    for title, agent_label, priority_label in test_issues:
        labels = ["test-data", agent_label]
        if priority_label:
            labels.append(priority_label)
        try:
            adapter.create_label(agent_label, force=True)
            if priority_label:
                adapter.create_label(priority_label, force=True)
            issue_number = adapter.create_issue(
                title=title,
                body="Test issue for orchestrator.\n\nExpected: Agent completes.",
                labels=labels,
            )
            if issue_number:
                console.print(f"  Created: https://github.com/{repo}/issues/{issue_number}")
        except Exception as exc:
            logger.warning("Failed to create test issue '%s': %s", title, exc)

    return True


def _apply_cli_overrides(args: argparse.Namespace, config: "Config") -> None:  # noqa: C901, PLR0912 - one branch per CLI override, inherent to config mapping
    """Apply CLI argument overrides to config."""
    # Handle milestone override
    if hasattr(args, "milestones") and args.milestones:
        milestones = [m.strip() for m in args.milestones.split(",") if m.strip()]
        config.filtering.milestones = milestones
        config.filtering.milestone = None
        console.print(f"[cyan]Filtering by milestones: {', '.join(milestones)}[/cyan]")
    elif hasattr(args, 'milestone') and args.milestone:
        config.filtering.milestone = args.milestone
        config.filtering.milestones = []
        console.print(f"[cyan]Filtering by milestone: {args.milestone}[/cyan]")

    # Handle label override
    if hasattr(args, 'label') and args.label:
        config.filtering.label = args.label
        console.print(f"[cyan]Filtering by label: {args.label}[/cyan]")

    # Handle single issue filter
    if hasattr(args, 'issue') and args.issue:
        config.filtering.issue = args.issue
        console.print(f"[cyan]Processing only issue #{args.issue}[/cyan]")

    # Handle ui_mode override
    if hasattr(args, 'ui_mode') and args.ui_mode:
        config.ui_mode = args.ui_mode
    console.print(f"[dim]UI mode: {config.ui_mode}[/dim]")

    # Handle queue_refresh override
    if hasattr(args, 'queue_refresh') and args.queue_refresh is not None:
        config.queue_refresh_seconds = args.queue_refresh

    # Handle GH audit overrides
    if hasattr(args, 'gh_audit') and args.gh_audit:
        config.gh_audit_enabled = True
    if hasattr(args, 'gh_audit_events') and args.gh_audit_events:
        config.gh_audit_events = True
    if hasattr(args, 'gh_audit_file') and args.gh_audit_file is not None:
        config.gh_audit_file = args.gh_audit_file

    # Handle max_issues override
    if hasattr(args, 'max_issues') and args.max_issues is not None:
        config.filtering.max_to_start = args.max_issues
        if config.filtering.max_to_start > 0:
            console.print(f"[dim]Max issues to start: {config.filtering.max_to_start}[/dim]")

    # Handle review workflow overrides
    if hasattr(args, 'review_label') and args.review_label is not None:
        config.triage_review_label = args.review_label
        console.print(f"[dim]Review label: {config.triage_review_label}[/dim]")
    if hasattr(args, 'review_threshold') and args.review_threshold is not None:
        config.triage_review_threshold = args.review_threshold
        if config.triage_review_threshold > 0:
            console.print(f"[dim]Review threshold: {config.triage_review_threshold} PRs[/dim]")


def _run_dry_run(args: argparse.Namespace, config: "Config") -> int:
    """Run dry-run mode - show what would be processed without starting."""
    from ..control.scheduler import Scheduler
    from ..execution.providers import create_repository_host
    from ..infra.analysis import analyze_all_issues, extract_issue_branches
    from ..execution.git_working_copy import GitWorkingCopy

    console.print("\n[cyan]DRY RUN - showing what would be processed:[/cyan]\n")

    scheduler = Scheduler(config)
    github = create_repository_host(config.repo) if config.repo else None
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
        session_status = "[green]active[/green]" if state.has_session else "[dim]none[/dim]"
        branch_status = (
            f"[cyan]{state.branch[:20]}...[/cyan]"
            if state.branch and len(state.branch) > 20
            else f"[cyan]{state.branch}[/cyan]" if state.branch else "[dim]none[/dim]"
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


def _print_dry_run_summary(states: list, all_issues: list, scheduler, config: "Config") -> None:
    """Print summary statistics for dry-run mode."""
    available, _ = scheduler.get_available_issues(all_issues, check_dependencies=False)
    console.print(f"\n[dim]Total issues: {len(all_issues)}[/dim]")
    console.print(f"[dim]Available to process: {len(available)}[/dim]")
    console.print(f"[dim]Would launch up to {config.max_concurrent_sessions} concurrent sessions[/dim]")

    # Warnings for stale issues
    stale_states = [s for s in states if s.is_stale]
    if stale_states:
        console.print(f"\n[yellow]Warning: {len(stale_states)} issue(s) marked in-progress but have no active session:[/yellow]")
        for state in stale_states:
            if state.branch:
                console.print(f"  [yellow]#{state.issue.number}[/yellow]: {state.issue.title[:35]} [cyan](has branch: {state.branch})[/cyan]")
            else:
                console.print(f"  [yellow]#{state.issue.number}[/yellow]: {state.issue.title[:40]}")
        console.print("\n[dim]Options:[/dim]")
        console.print("[dim]  - Reset to restart fresh: gh issue edit # --remove-label in-progress[/dim]")
        console.print("[dim]  - Resume from branch: orchestrator will checkout existing branch if present[/dim]")


def _print_orphan_branches(states: list, config: "Config", github, working_copy) -> None:
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
        commits_ahead_fn=lambda b: working_copy.get_commits_ahead_count(config.repo_root, b),
        last_commit_date_fn=lambda b: working_copy.get_last_commit_date(config.repo_root, b),
    )

    if not orphan_states:
        return

    console.print(f"\n[yellow]Warning: {len(orphan_states)} orphan branch(es) found:[/yellow]")

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
    delete_count = sum(1 for o in orphan_states if o.suggested_action == "delete-branch")
    if resume_count > 0:
        console.print(f"\n[dim]To resume work on open issues, add in-progress label:[/dim]")
        console.print(f"[dim]  gh issue edit # --add-label {config.get_label_in_progress()}[/dim]")
    if delete_count > 0:
        console.print(f"\n[dim]To clean up stale branches:[/dim]")
        console.print(f"[dim]  git push origin --delete <branch-name>[/dim]")


def _format_orphan_issue_info(orphan) -> str:
    """Format issue info for orphan branch display."""
    if orphan.issue_title:
        title_short = orphan.issue_title[:25] + ("..." if len(orphan.issue_title) > 25 else "")
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
    if api_port:
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


async def _run_web_dashboard(orchestrator, config: "Config", args: argparse.Namespace, api_port: int | None) -> None:
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
    if api_port:
        console.print(f"[dim]Control API on http://127.0.0.1:{api_port}[/dim]")
        control_api = ControlAPIServer(orchestrator, port=api_port)
        try:
            await control_api.start()
        except OSError as exc:
            logging.warning("Control API failed to start on port %s: %s", api_port, exc)
            control_api = None

    try:
        port = args.port if args.port != 8080 else config.web_port
        await run_with_web_dashboard(orchestrator, port=port)
    finally:
        if control_api:
            await control_api.stop()


async def _run_tui_dashboard(orchestrator, config: "Config", api_port: int | None) -> bool:
    """Run orchestrator with TUI dashboard."""
    from .control_api import ControlAPIServer
    from .dashboard import run_with_dashboard

    control_api = None
    if api_port:
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
    debug = getattr(args, 'debug', False)
    no_dashboard = getattr(args, 'no_dashboard', False)
    log_level = "DEBUG" if debug else "INFO"

    console.print("[green]Starting issue-orchestrator...[/green]")

    try:
        from .bootstrap import build_orchestrator

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
        launch_result = launch_preflight_only(config=config, runner=LocalCommandRunner())
        if launch_result.status == "doctor_error":
            console.print("[red]Startup checks failed:[/red]")
            for check in launch_result.doctor.checks:
                if check.status == "error":
                    console.print(f"  [red]✗ {check.name}: {check.detail}[/red]")
                    logging.error(f"Doctor check failed: {check.name}: {check.detail}")
            console.print("\n[yellow]Run 'issue-orchestrator doctor' for full diagnostics[/yellow]")
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
    console.print(f"[dim]Max concurrent sessions: {config.max_concurrent_sessions}[/dim]")

    # Handle dry-run mode
    if hasattr(args, 'dry_run') and args.dry_run:
        return _run_dry_run(args, config)

    orchestrator = build_orchestrator(config=config)

    # Get control API port (CLI --api-port overrides config)
    api_port = getattr(args, 'api_port', None) or config.control_api_port

    try:
        if args.no_dashboard:
            # Run orchestrator without dashboard (useful for CI/debugging)
            console.print("[dim]Running without dashboard UI[/dim]")
            if api_port:
                console.print(f"[dim]Control API on http://127.0.0.1:{api_port}[/dim]")
            asyncio.run(_run_no_dashboard(orchestrator, api_port))
        elif config.ui_mode == "web":
            # Run with web dashboard in browser
            port = args.port if args.port != 8080 else config.web_port
            console.print("[dim]Starting web dashboard...[/dim]")
            console.print(f"[green]Dashboard will open at http://localhost:{port}[/green]")
            asyncio.run(_run_web_dashboard(orchestrator, config, args, api_port))
        else:
            # Run with interactive TUI dashboard (tmux mode)
            if api_port:
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
            console.print(f"  Filter milestones: {', '.join(config.filtering.milestones)}")
        elif config.filtering.milestone:
            console.print(f"  Filter milestone: {config.filtering.milestone}")

        console.print("\n[dim]Note: Use the web dashboard to view active sessions[/dim]")
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
    console.print("Start the orchestrator to access the web dashboard: issue-orchestrator start")
    return 1


def cmd_output(args: argparse.Namespace) -> int:
    """Show recent output from an issue's session."""
    console.print("[yellow]The 'output' command is no longer available.[/yellow]")
    console.print("View session logs in .issue-orchestrator/sessions/<session>/session.log")
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
            console.print(f"[yellow]Orchestrator responded with {resp.status_code}[/yellow]")
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
                console.print(f"[dim]Still waiting... ({i+1}s)[/dim]")
        except httpx.ConnectError:
            # Orchestrator has exited
            console.print("[green]Orchestrator stopped[/green]")
            break
    else:
        console.print("[yellow]Orchestrator didn't stop in time, continuing anyway...[/yellow]")

    # Step 4: Start new orchestrator
    console.print("[cyan]Starting new orchestrator...[/cyan]")
    return _start_fresh(args)


def _start_fresh(args: argparse.Namespace) -> int:
    """Start a fresh orchestrator instance."""
    import sys

    # Build command to run start
    cmd = [sys.executable, "-m", "issue_orchestrator.entrypoints.cli", "start"]

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

    from .cli_tools.setup_wizard import run_wizard

    target_path = Path(args.path).expanduser().resolve() if args.path else None
    dry_run = getattr(args, 'dry_run', False)
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
    existing = {label.get("name") for label in client.list_labels() if isinstance(label, dict)}

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
        console.print("\n[yellow]Some labels failed to create. Check your GitHub token/auth.[/yellow]")
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


def _load_config(args: argparse.Namespace) -> "Config":
    """Load config from explicit path or search for it.

    Args:
        args: Parsed command line arguments

    Returns:
        Loaded Config object

    Raises:
        FileNotFoundError: If config file not found
    """
    from ..infra.config import Config

    overrides = getattr(args, "set", None) or []
    if hasattr(args, 'config') and args.config:
        config_path = Path(args.config)
        # Config.load() handles repo_root calculation properly
        return Config.load(config_path, overrides=overrides)
    else:
        return Config.find_and_load(overrides=overrides)


def cmd_audit(args: argparse.Namespace) -> int:
    """Audit the queue - show why issues are queued or skipped."""
    from ..infra.audit import audit_queue, print_audit
    from ..execution.providers import create_repository_host
    from ..execution.git_working_copy import GitWorkingCopy
    from ..infra.analysis import extract_issue_branches

    console.print("[bold]Queue Audit[/bold]\n")

    # Load config
    try:
        config = _load_config(args)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1

    console.print(f"[dim]Repository: {config.repo}[/dim]")
    console.print(f"[dim]Agents: {', '.join(config.agents.keys())}[/dim]")

    if not config.repo:
        console.print("[red]Error: No repository configured[/red]")
        return 1

    # Run audit (no state = fresh start, no session history)
    issue_tracker = create_repository_host(config.repo)
    working_copy = GitWorkingCopy()
    issue_branches = extract_issue_branches(
        working_copy.list_remote_branches(config.repo_root)
    )
    entries = audit_queue(
        config,
        state=None,
        issue_tracker=issue_tracker,
        issue_branches=issue_branches,
    )
    print_audit(entries)

    return 0


def cmd_verify(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912 - multi-step verification: config, git, GitHub, tmux, agents
    """Verify the orchestrator setup works correctly."""
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
    from ..execution.git_working_copy import GitWorkingCopy
    working_copy = GitWorkingCopy()
    if working_copy.is_git_repo(config.repo_root):
        console.print(f"  [green]✓[/green] Valid git repository")
    else:
        console.print(f"  [red]✗[/red] Not a git repository: {config.repo_root}")
        errors.append("Not a git repository")

    # 3. Check GitHub API auth
    console.print("\n[bold]3. GitHub API Auth[/bold]")
    try:
        client = _get_repository_host(config)
        if client is None:
            console.print("  [red]✗[/red] GitHub client could not be created")
            errors.append("GitHub token missing or invalid")
        else:
            snapshot = client.get_rate_limit_snapshot()
            if snapshot:
                console.print("  [green]✓[/green] GitHub token authenticated")
            else:
                console.print("  [yellow]![/yellow] GitHub token not verified (no response)")
                warnings.append("GitHub token could not be verified")
    except Exception as exc:
        console.print(f"  [red]✗[/red] GitHub auth failed: {exc}")
        errors.append("GitHub token missing or invalid")

    # 4. Check hooks setup
    console.print("\n[bold]4. Git Hooks[/bold]")
    from ..execution.providers import get_hooks_dir

    bundled_hook = get_hooks_dir() / "pre-push"
    if bundled_hook.exists():
        console.print(f"  [green]✓[/green] Bundled pre-push hook exists")
    else:
        console.print(f"  [red]✗[/red] Bundled pre-push hook missing: {bundled_hook}")
        errors.append("Bundled pre-push hook not found")

    # Check if project uses custom hooksPath
    custom_path = working_copy.get_config_value(config.repo_root, "core.hooksPath")
    if custom_path:
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

    # 6. Terminal backend
    console.print("\n[bold]6. Terminal Backend[/bold]")
    backend = config.terminal_adapter or "subprocess"
    console.print(f"  [green]✓[/green] Using terminal backend: {backend}")
    console.print(f"  [cyan]ℹ[/cyan] Sessions run as subprocesses with output logged to session.log")

    # 7. Verify AI agent hooks
    console.print("\n[bold]7. AI Agent Hooks[/bold]")
    from ..infra.hooks.hooks import (
        detect_agents_from_config,
        get_adapter,
        UnsupportedAiAgentError,
    )

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    console.print(f"  [cyan]ℹ[/cyan] Detected AI agents: {[t.value for t in unique_types]}")

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

        except UnsupportedAiAgentError as e:
            console.print(f"  [red]✗[/red] {agent_type.value}: {e.reason}")
            errors.append(f"Unsupported AI agent: {e.reason}")

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


def cmd_setup_hooks(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912 - multi-step setup with per-agent install, verify, and safety check
    """Install AI agent hooks for the target project."""
    from ..infra.hooks.hooks import (
        detect_agents_from_config,
        get_adapter,
        UnsupportedAiAgentError,
    )
    from ..infra.safety_state import load_safety_state, save_safety_state

    console.print("[bold cyan]Installing AI Agent Hooks[/bold cyan]\n")

    # Load config
    try:
        config = _load_config(args)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("No config found. Run 'issue-orchestrator setup' to create one.")
        return 1

    # Detect AI agents from config
    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    console.print(f"[bold]Detected AI Agents:[/bold]")
    for agent_label, agent_type in agent_types.items():
        console.print(f"  {agent_label} → {agent_type.value}")

    console.print()

    # Determine target directory
    target_root = Path(args.target).resolve() if hasattr(args, 'target') and args.target else config.repo_root

    console.print(f"[bold]Target Project:[/bold] {target_root}\n")

    errors = []
    installed = []
    supported_adapters = []
    verification_failures = []

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
                console.print(f"  [green]✓[/green] Static verification passed ({len(result.checks_passed)} checks)")
                supported_adapters.append((agent_type, adapter))
            else:
                console.print(f"  [yellow]![/yellow] Verification had issues:")
                for failure in result.checks_failed:
                    console.print(f"    [red]✗[/red] {failure}")
                verification_failures.append(agent_type.value)

        except UnsupportedAiAgentError as e:
            console.print(f"  [red]✗[/red] {agent_type.value}: {e.reason}")
            errors.append(str(e))

    console.print()

    if errors:
        console.print(f"[bold red]Setup completed with {len(errors)} error(s)[/bold red]")
        console.print("\n[yellow]Some AI agents are not supported. Consider using Claude Code.[/yellow]")
        return 1

    console.print(f"[green]✓[/green] Files installed")
    if verification_failures:
        console.print(f"[yellow]![/yellow] Static verification failed for: {', '.join(verification_failures)}")
    else:
        console.print(f"[green]✓[/green] Static verification passed")

    # Run safety check (live verification)
    if supported_adapters:
        console.print("\n[cyan]Running safety check (spawns agent briefly)...[/cyan]")
        safety_results: dict[str, tuple[bool, str]] = {}
        safety_failures = []

        for agent_type, adapter in supported_adapters:
            agent_name = agent_type.value
            try:
                success, message = adapter.live_verify(target_root)
                safety_results[agent_name] = (success, message)

                if success:
                    # Extract the blocked command from the message if available
                    detail = message.split("\n")[0] if message else "blocked"
                    console.print(f"[green]✓[/green] {agent_name}: correctly {detail[:60]}")
                else:
                    console.print(f"[red]✗[/red] {agent_name}: {message[:60]}")
                    safety_failures.append(agent_name)
            except Exception as e:
                error_msg = str(e)
                safety_results[agent_name] = (False, error_msg)
                console.print(f"[red]✗[/red] {agent_name}: Error - {error_msg[:50]}")
                safety_failures.append(agent_name)

        # Save safety state
        state = load_safety_state(target_root)
        state.mark_checked(safety_results)
        save_safety_state(target_root, state)

        if safety_failures:
            console.print()
            if config.hooks.safety_check.dangerous_allow_failure:
                console.print(f"[bold yellow]⚠ Safety check failed for: {', '.join(safety_failures)}[/bold yellow]")
                console.print("[dim]Continuing because dangerous_allow_failure is enabled[/dim]")
            else:
                console.print(f"[bold red]Safety check failed for: {', '.join(safety_failures)}[/bold red]")
                console.print("\n[yellow]Hooks installed but safety check failed.[/yellow]")
                console.print("[dim]Set hooks.safety_check.dangerous_allow_failure: true to bypass[/dim]")
                return 1

    console.print()
    if verification_failures:
        console.print(f"[bold yellow]Hooks installed (verification failed for {len(verification_failures)} agent(s)).[/bold yellow]")
        return 1
    console.print(f"[bold green]Hooks installed and verified.[/bold green]")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    """Manage GitHub authentication."""
    from rich.console import Console
    console = Console()

    action = getattr(args, "auth_action", None)
    if action is None:
        console.print("[yellow]Usage: issue-orchestrator auth <store|clear>[/yellow]")
        console.print("[dim]For diagnostics, use: issue-orchestrator doctor[/dim]")
        return 1

    if action == "store":
        return _cmd_auth_store(args, console)
    elif action == "clear":
        return _cmd_auth_clear(args, console)
    else:
        console.print(f"[red]Unknown auth action: {action}[/red]")
        return 1


def _cmd_auth_store(args: argparse.Namespace, console) -> int:
    """Store GitHub token in OS keychain."""
    from ..execution.providers import store_keyring_token
    import getpass

    token = getattr(args, "token", None)
    if not token:
        try:
            token = getpass.getpass("Enter GitHub token: ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Cancelled[/yellow]")
            return 1

    if not token or not token.strip():
        console.print("[red]Token cannot be empty[/red]")
        return 1

    try:
        store_keyring_token(token.strip())
        console.print("[green]✓ Token stored in OS keychain[/green]")
        console.print("[dim]The token will be used when ISSUE_ORCH_GITHUB_TOKEN is not set.[/dim]")
        return 0
    except ImportError:
        console.print("[red]Error: keyring library not installed[/red]")
        console.print("[dim]Install with: pip install keyring[/dim]")
        return 1
    except Exception as e:
        console.print(f"[red]Failed to store token: {e}[/red]")
        return 1


def _cmd_auth_clear(args: argparse.Namespace, console) -> int:
    """Clear GitHub token from OS keychain."""
    from ..execution.providers import clear_keyring_token

    if clear_keyring_token():
        console.print("[green]✓ Token cleared from OS keychain[/green]")
    else:
        console.print("[yellow]No token was stored in keychain[/yellow]")
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Manage AI provider API keys."""
    action = getattr(args, "keys_action", None)
    if action is None:
        console.print("[yellow]Usage: issue-orchestrator keys <list|set|delete>[/yellow]")
        return 1

    if action == "list":
        return _cmd_keys_list(args)
    elif action == "set":
        return _cmd_keys_set(args)
    elif action == "delete":
        return _cmd_keys_delete(args)
    else:
        console.print(f"[red]Unknown keys action: {action}[/red]")
        return 1


def _cmd_keys_list(args: argparse.Namespace) -> int:
    """List stored AI provider API keys."""
    from ..infra.ai_keys import list_ai_keys, AI_PROVIDERS

    keys = list_ai_keys()
    console.print("\n[bold]AI Provider Keys:[/bold]")
    for key_name, (masked, source) in keys.items():
        provider_info = AI_PROVIDERS.get(key_name, {})
        provider_name = provider_info.get("name", key_name)
        if source == "not set":
            console.print(f"  {provider_name} ({key_name}): [dim]not configured[/dim]")
        else:
            console.print(f"  {provider_name} ({key_name}): {masked} [dim]({source})[/dim] ✓")
    console.print()
    return 0


def _cmd_keys_set(args: argparse.Namespace) -> int:
    """Store an AI provider API key in keyring."""
    from ..infra.ai_keys import store_ai_key, AI_PROVIDERS
    import getpass

    key_name = args.key_name.upper()
    # Normalize: 'anthropic' -> 'ANTHROPIC_API_KEY'
    if not key_name.endswith("_API_KEY"):
        key_name = f"{key_name}_API_KEY"

    # Show setup help for known providers
    if key_name in AI_PROVIDERS:
        info = AI_PROVIDERS[key_name]
        console.print(f"\n[bold]{info['name']}[/bold]")
        if info.get("setup_cmd"):
            console.print(f"  Run in another terminal: [cyan]{info['setup_cmd']}[/cyan]")
            console.print("  Then paste the key here.")
        else:
            console.print(f"  {info.get('setup_help', info.get('url', ''))}")
        console.print()

    # Prompt for key
    try:
        value = getpass.getpass(f"Paste your {key_name}: ")
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled[/yellow]")
        return 1

    if not value.strip():
        console.print("[red]No key provided[/red]")
        return 1

    try:
        store_ai_key(key_name, value.strip())
        console.print(f"[green]✓ Stored {key_name} in keyring[/green]")
        return 0
    except ImportError:
        console.print("[red]Error: keyring library not installed[/red]")
        console.print("[dim]Install with: pip install keyring[/dim]")
        return 1
    except Exception as e:
        console.print(f"[red]Failed to store key: {e}[/red]")
        return 1


def _cmd_keys_delete(args: argparse.Namespace) -> int:
    """Delete an AI provider API key from keyring."""
    from ..infra.ai_keys import delete_ai_key

    key_name = args.key_name.upper()
    if not key_name.endswith("_API_KEY"):
        key_name = f"{key_name}_API_KEY"

    if delete_ai_key(key_name):
        console.print(f"[green]✓ Removed {key_name} from keyring[/green]")
    else:
        console.print(f"[yellow]{key_name} was not in keyring[/yellow]")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run unified diagnostics on configuration and environment."""
    from pathlib import Path
    from rich.console import Console
    from ..infra.doctor import run_doctor
    from ..execution.command_runner import LocalCommandRunner

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
    elif result.overall == "warning":
        console.print("[yellow]Completed with warnings[/yellow]")
        return 0
    else:
        console.print("[green]All checks passed[/green]")
        return 0


def cmd_demo(args: argparse.Namespace) -> int:  # noqa: C901 - demo flow with dry-run/live modes and feature showcases
    """Demonstrate orchestrator features with mock data.

    Behavior per DEMO_CONTRACT.md:
    - If ISSUE_ORCH_GITHUB_TOKEN is not set: runs dry-run with local fixtures
    - If token set and repo configured: creates demo issue and runs cycle
    """
    import os
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from pathlib import Path

    from ..control.scheduler import Scheduler
    from ..control.dependency_evaluator import DependencyEvaluator
    from ..domain.dependencies import parse_dependencies
    from ..infra.config import Config
    from ..domain.models import Issue, AgentConfig  # Issue used for demo mock creation

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
            # All mock issues are in M1 for demo purposes
            self.milestones = {1: "M1", 2: "M1", 3: "M1", 4: "M1", 5: "M1"}

        def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
            return self.states.get(issue_number)

        def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
            return self.milestones.get(issue_number)

    class CollectingEventSink:
        """Collects events for display."""
        def __init__(self):
            self.events = []
        def publish(self, event):
            self.events.append(event)

    checker = MockIssueChecker()
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
    console.print("[bold]Scenario:[/bold] Issue #1 is CLOSED (completed), issues #2-5 are OPEN")
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


def cmd_default(args: argparse.Namespace) -> int:  # noqa: ARG001 - args unused but required for command signature
    """Default command when no subcommand is given - open unified dashboard."""
    import webbrowser

    from ..observation.instance_detector import detect_system_state, get_best_entry_point

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
        from urllib.parse import quote

        port = entry["port"]
        repo_path = entry.get("repo_path")

        # Build URL with optional deep-link
        if repo_path:
            url = f"http://localhost:{port}?repo={quote(repo_path)}"
        else:
            url = f"http://localhost:{port}"

        # Start control center as a subprocess
        cmd = [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.control_center",
            "--port",
            str(port),
            "--no-browser",  # We'll open browser ourselves
        ]

        # Start in background
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Wait a bit for server to start
        time.sleep(1.5)

        # Check if process is still running
        if process.poll() is not None:
            console.print("[red]Failed to start dashboard server[/red]")
            return 1

        console.print(f"[green]Dashboard started on {url}[/green]")
        webbrowser.open(url)
        return 0


def cmd_trace(args: argparse.Namespace) -> int:  # noqa: C901 - log parsing with pattern matching and filtering logic
    """Trace log entries for a specific issue."""
    import re

    issue_number = args.issue_number

    # Find the log file by walking up from cwd to find repo root
    def find_log_file() -> Path | None:
        current = Path.cwd()
        for _ in range(10):  # Max 10 levels up
            candidate = current / ".issue-orchestrator" / "state" / "logs" / "orchestrator.log"
            if candidate.exists():
                return candidate
            if current.parent == current:
                break
            current = current.parent
        return None

    log_file = find_log_file()

    if log_file is None:
        console.print("[red]Error: orchestrator.log not found[/red]")
        console.print("Run this command from within a repository that has the orchestrator running.")
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
        console.print("[yellow]Warning: No startup marker found, showing all entries[/yellow]", style="dim")

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


def main() -> int:
    """Main entry point for the CLI."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Orchestrate AI agents working on GitHub issues"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to config file (default: .issue-orchestrator/config/default.yaml)"
    )
    parser.add_argument(
        "--set",
        action="append",
        help="Override config value (path=value). Use YAML/JSON for lists or dicts.",
    )
    subparsers: Any = parser.add_subparsers(
        dest="command", required=False
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
        "--milestones",
        type=str,
        default=None,
        help="Filter issues by milestone names (comma-separated)"
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
        choices=["web"],
        default=None,
        help="UI mode: web (browser dashboard, default)"
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for web dashboard (default: 8080)"
    )
    start_parser.add_argument(
        "--api-port",
        type=int,
        default=None,
        dest="api_port",
        help="Port for control API (default: 19080, 0=disabled). Control API is always available regardless of UI mode."
    )
    start_parser.add_argument(
        "--queue-refresh",
        type=int,
        default=None,
        help="Seconds between queue refreshes from GitHub (default: 600, 0=manual only)"
    )
    start_parser.add_argument(
        "--gh-audit",
        action="store_true",
        help="Enable GH audit reporting (overrides config)"
    )
    start_parser.add_argument(
        "--gh-audit-events",
        action="store_true",
        help="Emit GH audit events to the event stream (overrides config)"
    )
    start_parser.add_argument(
        "--gh-audit-file",
        type=str,
        default=None,
        help="Path for GH audit report output (supports {pid})"
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
        help="Optional: switch to this issue's window after attaching"
    )
    attach_parser.set_defaults(func=cmd_attach)

    # switch command (deprecated)
    switch_parser: argparse.ArgumentParser = subparsers.add_parser(
        "switch", help="(deprecated) Use web dashboard instead"
    )
    switch_parser.add_argument(
        "issue_number",
        type=int,
        help="GitHub issue number to switch to"
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
        "restart", help="Restart the orchestrator"
    )
    restart_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port of running orchestrator (default: 8080)"
    )
    restart_parser.add_argument(
        "--ui-mode",
        choices=["web"],
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
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what files would be created/modified without writing them"
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
        "setup-hooks", help="Install AI agent hooks in target project"
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

    auth_subparsers.add_parser(
        "clear", help="Clear GitHub token from OS keychain"
    )

    auth_parser.set_defaults(func=cmd_auth)

    # keys command (AI provider API keys)
    keys_parser: argparse.ArgumentParser = subparsers.add_parser(
        "keys", help="Manage AI provider API keys"
    )
    keys_subparsers = keys_parser.add_subparsers(dest="keys_action")

    keys_subparsers.add_parser(
        "list", help="List stored API keys"
    )

    keys_set_parser = keys_subparsers.add_parser(
        "set", help="Store an API key in keyring"
    )
    keys_set_parser.add_argument(
        "key_name",
        help="Key name (e.g., ANTHROPIC_API_KEY or just 'anthropic')"
    )

    keys_delete_parser = keys_subparsers.add_parser(
        "delete", help="Remove an API key from keyring"
    )
    keys_delete_parser.add_argument(
        "key_name",
        help="Key name to remove"
    )

    keys_parser.set_defaults(func=cmd_keys)

    # doctor command (unified diagnostics)
    doctor_parser: argparse.ArgumentParser = subparsers.add_parser(
        "doctor", help="Run diagnostics on configuration and environment"
    )
    doctor_parser.add_argument(
        "--config", "-c", type=str, help="Path to config file"
    )
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
    trace_parser.add_argument(
        "issue_number",
        type=int,
        help="Issue number to trace"
    )
    trace_parser.set_defaults(func=cmd_trace)

    args: argparse.Namespace = parser.parse_args()

    # If no command specified, run the default (open dashboard)
    if args.command is None:
        return cmd_default(args)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
