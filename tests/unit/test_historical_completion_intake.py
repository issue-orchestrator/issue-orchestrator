"""Historical import through real custody, allocation, validation and admission owners."""

from issue_orchestrator.control.validated_work_admission import RankedEvidenceAdmission

from issue_orchestrator.infra.config import Config

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

from issue_orchestrator.control.completion_intake_validation import (
    ConfiguredCompletionEvidenceValidator,
)
from issue_orchestrator.control.historical_completion_intake import (
    HistoricalCompletionIntake,
)
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.domain.historical_intake import (
    HistoricalIntakeCommand,
    HistoricalIntakeRefusal,
    HistoricalIntakeParked,
    HistoricalIntakeRefused,
    HistoricalIntakeValidationFailed,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.historical_intake_custody import (
    HistoricalIntakeCustody,
    IsolatedCompletionValidationWorkspace,
)
from issue_orchestrator.execution.intake_disposition_verification import (
    IntakeDispositionVerification,
)
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.validated_work_intake_store import (
    SqliteValidatedWorkIntakeStore,
)
from issue_orchestrator.ports.command_runner import CommandResult, CommandRunner
from tests.unit.test_completion_evidence_intake import completion


def historical(tmp_path: Path, *, prepare=None):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git = create_git(LocalCommandRunner())
    git.run(repo, ["init", "-b", "feature"])
    (repo / ".gitignore").write_text(".issue-orchestrator/\n")
    git.run(repo, ["add", ".gitignore"])
    git.run(
        repo,
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
    )
    head = git.head_sha(repo)
    state = repo / ".issue-orchestrator" / "state"
    ledger = SqliteIssueRunLedger(state / "issue_run_ledger.sqlite")
    output = FileSystemSessionOutput()
    wc = GitWorkingCopy(git=git)
    allocator = IssueRunAllocationService(output, ledger, wc, configuration=Config(repo="test/repo"))
    runner = Mock(spec=CommandRunner)
    runner.run.return_value = CommandResult(
        returncode=0, stdout="fresh validation", stderr="", timed_out=False
    )
    validator = ConfiguredCompletionEvidenceValidator(
        wc,
        runner,
        IsolatedCompletionValidationWorkspace(
            state, git, prepare or (lambda _path: None)
        ),
        command="configured-validation",
        timeout_seconds=30,
    )
    from issue_orchestrator.infra.validated_work_escrow import FilesystemValidatedWorkEscrow
    from issue_orchestrator.execution.validated_work_ancestry import GitValidatedWorkAncestry
    from issue_orchestrator.control.validated_work_capture import ValidatedWorkCustody
    escrow = FilesystemValidatedWorkEscrow(state / "validated-work", repository=repo, repo_slug="test/repo", git=wc)
    store = SqliteValidatedWorkIntakeStore(state / "validated_work.sqlite",
        GitValidatedWorkAncestry(repository=repo, repo_slug="test/repo", git=wc), escrow)
    custody = HistoricalIntakeCustody(
        repo_root=repo, state_root=state, git=git, custody=ValidatedWorkCustody(escrow, store), ledger=ledger
    )
    owner = HistoricalCompletionIntake(
        repo_slug="test/repo",
        repo_root=repo,
        working_copy=wc,
        workspace=custody,
        allocator=allocator,
        ledger=ledger,
        validator=validator,
    )
    candidate = tmp_path / "historical.json"
    candidate.write_bytes(completion())
    command = HistoricalIntakeCommand(
        "test/repo",
        42,
        "feature",
        head,
        candidate,
        sha256(completion()).hexdigest(),
        "operator",
        "recover sidecar",
    )
    return owner, ledger, command, runner


def test_isolated_checkout_is_prepared_before_configured_validation(tmp_path):
    def prepare(workspace: Path) -> None:
        (workspace / ".git" / "runtime-ready").write_text("prepared")

    owner, _, command, runner = historical(tmp_path, prepare=prepare)

    assert isinstance(owner.import_historical(command), HistoricalIntakeParked)
    workspace = runner.run.call_args.kwargs["cwd"]
    assert (workspace / ".git" / "runtime-ready").read_text() == "prepared"


def test_workspace_setup_failure_is_a_retained_prerequisite_failure(tmp_path):
    def fail_setup(_workspace: Path) -> None:
        raise RuntimeError("setup failed")

    owner, _, command, runner = historical(tmp_path, prepare=fail_setup)

    assert owner.import_historical(command) == HistoricalIntakeRefused(
        HistoricalIntakeRefusal.PREREQUISITE_UNAVAILABLE
    )
    runner.run.assert_not_called()


def test_fresh_failed_validation_then_success_always_parks(tmp_path):
    owner, ledger, command, runner = historical(tmp_path)
    runner.run.return_value = CommandResult(
        returncode=1, stdout="failed", stderr="details", timed_out=False
    )
    failed = owner.import_historical(command)
    assert isinstance(failed, HistoricalIntakeValidationFailed)
    assert not ledger.validation_for_receipt(failed.entry_id).passed
    runner.run.return_value = CommandResult(
        returncode=0, stdout="passed", stderr="", timed_out=False
    )
    parked = owner.import_historical(command)
    assert isinstance(parked, HistoricalIntakeParked)
    assert runner.run.call_count == 2
    workspace = runner.run.call_args.kwargs["cwd"]
    assert workspace != command.candidate_path.parent
    assert workspace.is_relative_to(tmp_path / "repo" / ".issue-orchestrator" / "state")
    assert len(ledger.recorded_runs(42)) == 2
    assert command.candidate_path.read_bytes() == completion()


def test_exact_repository_path_hash_and_head_selection(tmp_path):
    owner, ledger, command, runner = historical(tmp_path)
    for invalid in (
        replace(command, repo_slug="wrong/repo"),
        replace(command, candidate_sha256="0" * 64),
        replace(command, target_head_sha="0" * 40),
        replace(command, branch_name="not-a-branch"),
    ):
        assert isinstance(owner.import_historical(invalid), HistoricalIntakeRefused)
    assert runner.run.call_count == 0
    assert ledger.recorded_runs(42) == ()


def test_restart_resumes_registered_historical_receipt_without_revalidation(tmp_path):
    import sqlite3
    from issue_orchestrator.control.completion_intake import (
        CompletionEvidenceIntakeService,
    )
    from issue_orchestrator.ports.background_job import BackgroundJobRunner
    from issue_orchestrator.ports.completion_intake import CompletionEvidenceValidator

    owner, ledger, command, runner = historical(tmp_path)
    db = tmp_path / "repo" / ".issue-orchestrator" / "state" / "validated_work.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER interrupt_park BEFORE INSERT ON validated_work_records BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
        )
    refused = owner.import_historical(command)
    assert refused == HistoricalIntakeRefused(
        HistoricalIntakeRefusal.PREREQUISITE_UNAVAILABLE
    )
    pending = ledger.pending_receipts()
    assert len(pending) == 1
    assert ledger.validation_for_receipt(pending[0].entry_id).passed
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER interrupt_park")
    service = CompletionEvidenceIntakeService(
        ledger,
        Mock(spec=CompletionEvidenceValidator),
        owner,
        Mock(spec=BackgroundJobRunner),
    )
    service.drain()
    assert ledger.pending_receipts() == ()
    assert runner.run.call_count == 1
    assert ledger.historical_command_for_receipt(pending[0].entry_id) == command
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute("SELECT state FROM validated_work_records").fetchone()[0]
            == "parked"
        )
