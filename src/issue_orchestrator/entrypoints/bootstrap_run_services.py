"""Compose IO adapters and the durable owner shared by every run allocator."""

from pathlib import Path

from ..control.issue_run_allocator import IssueRunAllocationService
from ..execution.issue_run_ledger import SqliteIssueRunLedger
from ..execution.worktree_adapter import GitWorktreeManager
from ..execution.git_working_copy import GitWorkingCopy
from ..execution.command_runner import LocalCommandRunner
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..execution.git_push_operations import GitAuthEnvProvider
from ..infra.repo_identity import state_dir
from ..ports.session_output import SessionOutput


def create_io_adapters(github_auth: GitAuthEnvProvider | None = None) -> tuple[
    GitWorktreeManager,
    GitWorkingCopy,
    LocalCommandRunner,
    FileSystemSessionOutput,
]:
    """Create IO adapter instances."""
    return (
        GitWorktreeManager(),
        GitWorkingCopy(git_auth=github_auth),
        LocalCommandRunner(),
        FileSystemSessionOutput(),
    )


def build_issue_run_services(
    repo_root: Path, session_output: SessionOutput,
) -> tuple[SqliteIssueRunLedger, IssueRunAllocationService]:
    """Use one ledger for allocation and the injected evidence reader."""
    ledger = SqliteIssueRunLedger(state_dir(repo_root) / "issue_run_ledger.sqlite")
    return ledger, IssueRunAllocationService(session_output, ledger)
