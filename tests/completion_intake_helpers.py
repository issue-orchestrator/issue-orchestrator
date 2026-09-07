"""Real receipt lifetime for component fixtures; only external ports are substituted."""

from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import (
    ConfiguredCompletionEvidenceValidator,
)
from issue_orchestrator.domain.completion_intake import (
    CompletionIntakeReceipt,
    IntakeClosed,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.command_runner import CommandResult, CommandRunner
from issue_orchestrator.ports.completion_intake import (
    CompletionIntakeRuntime,
    CompletionValidationWorkspace,
)
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler
from issue_orchestrator.ports.working_copy import WorkingCopy
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.test_issue_run_evidence import run_record


@dataclass(frozen=True)
class AcceptedCompletion:
    capability: str = field(repr=False)
    receipt: CompletionIntakeReceipt


@dataclass(frozen=True)
class CompletionIntakeFixture:
    root: Path
    ledger: SqliteIssueRunLedger
    runtime: CompletionIntakeRuntime

    def accept(self, issue_number: int) -> AcceptedCompletion:
        record = run_record(self.root / str(issue_number), run_id=f"run-{issue_number}")
        record = replace(
            record,
            session_key=SessionKey(
                FakeIssueKey(str(issue_number), "example/repo"), TaskKind.CODE
            ),
        )
        record.run.worktree_path.mkdir(parents=True)
        self.ledger.record_run(issue_number, record)
        capability = self.ledger.submission_capability(record.run)
        receipt = self.runtime.submit(capability, command(completion()))
        return AcceptedCompletion(capability, receipt)

    def assert_closed_and_drained(self, accepted: AcceptedCompletion) -> None:
        # A different key tests closed authority, not the allowed identical retry.
        with pytest.raises(IntakeClosed):
            self.runtime.submit(accepted.capability, command(b"late", "late"))
        assert accepted.receipt.entry_id not in {
            entry.entry_id for entry in self.ledger.pending_receipts()
        }
        result = self.ledger.validation_for_receipt(accepted.receipt.entry_id)
        assert result is not None and result.passed


def make_completion_intake_fixture(root: Path) -> CompletionIntakeFixture:
    ledger = SqliteIssueRunLedger(root / "state" / "issue_run_ledger.sqlite")
    working_copy = Mock(spec=WorkingCopy)
    working_copy.get_head_sha.return_value = "a" * 40
    working_copy.has_uncommitted_changes.return_value = False
    commands = Mock(spec=CommandRunner)
    commands.run.return_value = CommandResult(0, "validated", "", False)
    workspace = Mock(spec=CompletionValidationWorkspace)
    checkout = root / "isolated-validator"
    checkout.mkdir()
    workspace.checkout.return_value = checkout
    runtime = CompletionEvidenceIntakeService(
        ledger,
        ConfiguredCompletionEvidenceValidator(
            working_copy,
            commands,
            workspace,
            command="fixture-validation",
            timeout_seconds=30,
        ),
        Mock(spec=HistoricalIntakeHandler),
        Mock(spec=BackgroundJobRunner),
    )
    return CompletionIntakeFixture(root, ledger, runtime)
