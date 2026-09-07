"""Compose IO adapters and the durable owner shared by every run allocator."""

from ..control.issue_run_allocator import IssueRunAllocationService
from ..execution.issue_run_ledger import SqliteIssueRunLedger
from ..execution.worktree_adapter import GitWorktreeManager
from ..execution.git_working_copy import GitWorkingCopy
from ..execution.command_runner import LocalCommandRunner
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..execution.git_push_operations import GitAuthEnvProvider
from ..infra.repo_identity import state_dir
from ..ports.session_output import SessionOutput
from ..ports.command_runner import CommandRunner
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.issue_run_allocator import IssueRunAllocator
from ..ports.issue_run_evidence import IssueRunLedger
from ..ports.working_copy import WorkingCopy
from ..infra.config import Config


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
    config: Config, session_output: SessionOutput,
) -> tuple[SqliteIssueRunLedger, IssueRunAllocationService]:
    """Use one ledger for allocation and the injected evidence reader."""
    ledger = SqliteIssueRunLedger(state_dir(config.repo_root) / "issue_run_ledger.sqlite")
    return ledger, IssueRunAllocationService(session_output, ledger, configuration=config)


def build_completion_intake(
    config: Config,
    ledger: IssueRunLedger,
    allocator: IssueRunAllocator,
    working_copy: WorkingCopy,
    command_runner: CommandRunner,
) -> CompletionIntakeRuntime:
    """One owner is shared by submission, completion processing and terminal capture."""
    from ..execution.git_tools import create_git
    from ..execution.thread_background_job_runner import ThreadBackgroundJobRunner
    from ..control.completion_intake import CompletionEvidenceIntakeService
    from ..control.completion_intake_validation import (
        ConfiguredCompletionEvidenceValidator,
    )
    from ..control.historical_completion_intake import HistoricalCompletionIntake
    from ..execution.historical_intake_custody import (
        HistoricalIntakeCustody,
        IsolatedCompletionValidationWorkspace,
    )
    from ..execution.intake_disposition_verification import (
        IntakeDispositionVerification,
    )
    from ..infra.validated_work_intake_store import SqliteValidatedWorkIntakeStore

    root = state_dir(config.repo_root)
    git = create_git(command_runner)
    verification = IntakeDispositionVerification(config.repo_root, root, git)
    admission = SqliteValidatedWorkIntakeStore(
        root / "validated_work.sqlite", verification, verification
    )
    validator = ConfiguredCompletionEvidenceValidator(
        working_copy,
        command_runner,
        IsolatedCompletionValidationWorkspace(root, git),
        command=config.validation.quick.cmd,
        timeout_seconds=config.validation.quick.timeout_seconds,
    )
    historical = HistoricalCompletionIntake(
        repo_slug=config.repo,
        repo_root=config.repo_root,
        working_copy=working_copy,
        workspace=HistoricalIntakeCustody(
            repo_root=config.repo_root,
            state_root=root,
            git=git,
            admission=admission,
            ledger=ledger,
        ),
        allocator=allocator,
        ledger=ledger,
        validator=validator,
    )
    owner = CompletionEvidenceIntakeService(
        ledger, validator, historical, ThreadBackgroundJobRunner()
    )
    owner.pump()
    return owner
