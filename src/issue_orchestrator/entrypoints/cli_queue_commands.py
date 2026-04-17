"""Queue inspection CLI command handlers."""

import argparse

from rich.console import Console

from . import cli_support

console = Console()


def cmd_audit(args: argparse.Namespace) -> int:
    """Audit the queue - show why issues are queued or skipped."""
    from ..execution.git_working_copy import GitWorkingCopy
    from ..execution.providers import create_repository_host
    from ..infra.analysis import extract_issue_branches
    from ..infra.audit import audit_queue, print_audit

    console.print("[bold]Queue Audit[/bold]\n")

    # Load config
    try:
        config = cli_support.load_config(args)
    except FileNotFoundError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1

    console.print(f"[dim]Repository: {config.repo}[/dim]")
    console.print(f"[dim]Agents: {', '.join(config.agents.keys())}[/dim]")

    if not config.repo:
        console.print("[red]Error: No repository configured[/red]")
        return 1

    # Run audit (no state = fresh start, no session history)
    issue_tracker = create_repository_host(config.repo, config=config)
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
