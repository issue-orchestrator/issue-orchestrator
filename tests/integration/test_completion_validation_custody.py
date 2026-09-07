"""Real local Git/command/ledger validation without an HTTP listener or provider."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import (
    ConfiguredCompletionEvidenceValidator,
)
from issue_orchestrator.domain.completion_intake import CompletionIntakeError
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.historical_intake_custody import (
    IsolatedCompletionValidationWorkspace,
)
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.test_issue_run_evidence import run_record


def intake(tmp_path: Path, validation_command: str):
    record = run_record(tmp_path)
    worktree = record.run.worktree_path
    worktree.mkdir()
    git = create_git(LocalCommandRunner())
    git.run(worktree, ["init"])
    (worktree / "source.txt").write_text("original\n")
    git.run(worktree, ["add", "source.txt"])
    git.run(
        worktree,
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
    )
    state = tmp_path / "receipt-owner"
    ledger = SqliteIssueRunLedger(state / "issue_run_ledger.sqlite")
    ledger.record_run(42, record)
    owner = CompletionEvidenceIntakeService(
        ledger,
        ConfiguredCompletionEvidenceValidator(
            GitWorkingCopy(git),
            LocalCommandRunner(),
            IsolatedCompletionValidationWorkspace(state, git),
            command=validation_command,
            timeout_seconds=30,
        ),
        Mock(spec=HistoricalIntakeHandler),
        Mock(spec=BackgroundJobRunner),
    )
    receipt = owner.submit(
        ledger.submission_capability(record.run), command(completion())
    )
    return ledger, owner, receipt, git


@pytest.mark.parametrize("exit_code", [0, 1])
def test_owned_validation_output_does_not_dirty_isolated_checkout(tmp_path, exit_code):
    ledger, owner, receipt, git = intake(
        tmp_path, f"printf validated; printf diagnostic >&2; exit {exit_code}"
    )
    owner.close_and_drain(42)
    entry = ledger.entry_for_receipt(receipt.entry_id)
    attestation = ledger.validation_for_receipt(receipt.entry_id)
    assert attestation is not None
    assert attestation.passed is (exit_code == 0)
    result = json.loads(attestation.result_path.read_bytes())
    assert Path(result["stdout_path"]).read_bytes() == b"validated"
    assert Path(result["stderr_path"]).read_bytes() == b"diagnostic"
    assert attestation.result_path.is_relative_to(entry.raw_path.parent.parent)
    assert not attestation.result_path.is_relative_to(entry.run.worktree_path)
    workspaces = list(
        (tmp_path / "receipt-owner" / "completion-validation-workspaces").iterdir()
    )
    assert len(workspaces) == 1
    workspace = workspaces[0]
    assert (
        git.run(workspace, ["status", "--porcelain", "--untracked-files=all"]).stdout
        == ""
    )
    assert not (workspace / ".issue-orchestrator").exists()
    assert (
        git.run(workspace, ["rev-parse", "HEAD"]).stdout.strip() == attestation.head_sha
    )
    restarted = SqliteIssueRunLedger(
        tmp_path / "receipt-owner" / "issue_run_ledger.sqlite"
    )
    assert restarted.validation_for_receipt(receipt.entry_id) == attestation
    assert restarted.pending_receipts() == ()


@pytest.mark.parametrize(
    "validation_command",
    [
        "printf changed > source.txt",
        "printf generated > unexpected.txt",
        "mkdir -p .issue-orchestrator/validation; printf forged > .issue-orchestrator/validation/forged.json",
        "git -c user.name=Fixture -c user.email=fixture@example.invalid commit --allow-empty -m changed",
    ],
    ids=["tracked-edit", "untracked-output", "validation-path-output", "changed-head"],
)
def test_command_workspace_mutations_still_refuse_attestation(
    tmp_path, validation_command
):
    ledger, owner, receipt, _ = intake(tmp_path, validation_command)
    with pytest.raises(
        CompletionIntakeError, match="workspace changed during validation"
    ):
        owner.close_and_drain(42)
    assert ledger.validation_for_receipt(receipt.entry_id) is None
    assert [entry.receipt for entry in ledger.pending_receipts()] == [receipt]
