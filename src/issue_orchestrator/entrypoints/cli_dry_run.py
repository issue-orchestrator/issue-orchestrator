"""Dry-run presentation and analysis helpers for the CLI."""

import argparse
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from ..infra.config import Config

console = Console()

__all__ = ["run_dry_run"]


def run_dry_run(args: argparse.Namespace, config: "Config") -> int:
    """Show what would be processed without starting the orchestrator."""
    from ..control.scheduler import Scheduler
    from ..execution.git_working_copy import GitWorkingCopy
    from ..execution.providers import create_repository_host
    from ..infra.analysis import analyze_all_issues, extract_issue_branches

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

    issue_branches = extract_issue_branches(
        working_copy.list_remote_branches(config.repo_root)
    )
    states = analyze_all_issues(
        issues=all_issues,
        repo=config.repo,
        issue_branches=issue_branches,
        check_session_fn=lambda _: False,
    )

    states.sort(key=lambda s: s.issue.priority)

    _print_dry_run_table(states)
    _print_dry_run_summary(states, all_issues, scheduler, config)
    _print_orphan_branches(states, config, github, working_copy)

    return 0


def _print_dry_run_table(states: list[Any]) -> None:
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
    states: list[Any], all_issues: list[Any], scheduler: Any, config: "Config"
) -> None:
    """Print summary statistics for dry-run mode."""
    available, _ = scheduler.get_available_issues(all_issues, check_dependencies=False)
    console.print(f"\n[dim]Total issues: {len(all_issues)}[/dim]")
    console.print(f"[dim]Available to process: {len(available)}[/dim]")
    console.print(
        f"[dim]Would launch up to {config.max_concurrent_sessions} concurrent sessions[/dim]"
    )

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
    states: list[Any], config: "Config", github: Any, working_copy: Any
) -> None:
    """Print orphan branches analysis for dry-run mode."""
    from ..infra.analysis import analyze_orphan_branches, extract_issue_branches

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

    resume_count = sum(1 for o in orphan_states if o.suggested_action == "resume-work")
    delete_count = sum(
        1 for o in orphan_states if o.suggested_action == "delete-branch"
    )
    if resume_count > 0:
        console.print(
            f"\n[dim]To resume work on open issues, add in-progress label:[/dim]"
        )
        from ..control.label_manager import LabelManager

        label_manager = LabelManager(config)
        console.print(
            f"[dim]  gh issue edit # --add-label {label_manager.in_progress}[/dim]"
        )
    if delete_count > 0:
        console.print(f"\n[dim]To clean up stale branches:[/dim]")
        console.print(f"[dim]  git push origin --delete <branch-name>[/dim]")


def _format_orphan_issue_info(orphan: Any) -> str:
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


def _format_orphan_action(orphan: Any) -> str:
    """Format suggested action for orphan branch display."""
    action_styles = {
        "resume-work": "[green]resume[/green]",
        "investigate": "[yellow]investigate[/yellow]",
        "delete-branch": "[red]delete[/red]",
    }
    return action_styles.get(orphan.suggested_action) or str(orphan.suggested_action)
