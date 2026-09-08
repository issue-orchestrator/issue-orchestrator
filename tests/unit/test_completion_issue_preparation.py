"""Numeric issue binding precedes live and historical receipt processing."""

from issue_orchestrator.infra.config import Config

from dataclasses import replace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.domain.completion_intake import CompletionIntakeError
from issue_orchestrator.domain.historical_intake import HistoricalIntakeParked
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.completion_intake import CompletionEvidenceValidator
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.test_historical_completion_intake import historical
from tests.unit.test_validated_work_preservation import custody as custody


def test_issue_bound_preparation_processes_exact_receipt_without_closing(custody):
    receipt = custody.intake.submit(custody.capability, command(completion()))
    assert custody.ledger.validation_for_receipt(receipt.entry_id) is None
    candidate = custody.intake.prepare_receipt_for_issue(receipt, custody.run, 42)
    assert candidate.run == custody.ledger.recorded_run(custody.run)
    assert candidate.entry.receipt == receipt
    assert candidate.validation.head_sha == custody.git.head_sha(custody.worktree)
    assert custody.intake.prepare_receipt(receipt, custody.run) == candidate
    custody.intake.submit(custody.capability, command(completion(), "still-open"))


@pytest.mark.parametrize("substitution", ["issue", "receipt", "run", "bool", "zero"])
def test_substitution_refuses_before_validation_or_processing(custody, substitution):
    allocator = IssueRunAllocationService(FileSystemSessionOutput(), custody.ledger, custody.wc, configuration=Config(repo="owner/repo"))
    other = allocator.allocate(IssueRunAllocation(custody.worktree, custody.run.session_name, 43,
        SessionKey(GitHubIssueKey("owner/repo", "43"), TaskKind.CODE), "agent:test", "test", terminal_id="issue-43"))
    assert other.session_name == custody.run.session_name
    receipt = custody.intake.submit(custody.capability, command(completion()))
    supplied_receipt, supplied_run, issue = receipt, custody.run, 42
    if substitution == "issue":
        issue = 43
    elif substitution == "receipt":
        supplied_receipt = replace(receipt, content_sha256="0" * 64)
    elif substitution == "run":
        supplied_run = replace(custody.run, manifest=replace(custody.run.manifest,
            path=custody.run.run_dir / "different-manifest.json"))
        assert supplied_run.identity == custody.run.identity
    elif substitution == "bool":
        issue = True
    else:
        issue = 0
    pending = custody.ledger.pending_receipts()
    with pytest.raises(CompletionIntakeError):
        custody.intake.prepare_receipt_for_issue(supplied_receipt, supplied_run, issue)
    assert custody.ledger.pending_receipts() == pending
    assert custody.ledger.validation_for_receipt(receipt.entry_id) is None
    assert custody.store.for_issue(42).dispositions == ()
    custody.intake.submit(custody.capability, command(completion(), "still-open"))


@pytest.mark.parametrize("interrupted", [False, True])
def test_historical_receipt_uses_same_numeric_issue_binding(tmp_path, interrupted):
    owner, ledger, selection, runner = historical(tmp_path)
    if interrupted:
        runner.run.side_effect = KeyboardInterrupt("interrupted validator")
        with pytest.raises(KeyboardInterrupt, match="interrupted validator"):
            owner.import_historical(selection)
        runner.run.side_effect = None
    else:
        assert isinstance(owner.import_historical(selection), HistoricalIntakeParked)
    entry, = ledger.entries_for_issue(42)
    validator = Mock(spec=CompletionEvidenceValidator)
    service = CompletionEvidenceIntakeService(ledger, validator, owner, Mock(spec=BackgroundJobRunner))
    calls = runner.run.call_count
    with pytest.raises(CompletionIntakeError, match="allocated issue"):
        service.prepare_receipt_for_issue(entry.receipt, entry.run, 43)
    assert runner.run.call_count == calls
    candidate = service.prepare_receipt_for_issue(entry.receipt, entry.run, 42)
    assert candidate.entry.receipt == entry.receipt
    assert candidate.validation.head_sha == selection.target_head_sha
    assert candidate.run.terminal_binding.terminal_id is None
    assert candidate.role.issue_number == 42
    assert candidate.role.agent_label == "operator:historical"
    assert candidate.role.task is TaskKind.CODE
    assert runner.run.call_count == calls + int(interrupted)
    validator.validate.assert_not_called()
