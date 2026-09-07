"""Frozen role, branch and actual terminal share one durable allocation owner."""

from dataclasses import replace
import sqlite3

import pytest

from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.domain.completion_intake import CompletionIntakeError
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.issue_run_evidence import IssueRunEvidenceUnavailable, RunTerminalBinding
from issue_orchestrator.domain.registered_completion import CompletionRunRole
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.test_validated_work_preservation import custody as custody


@pytest.mark.parametrize("tech_lead", [False, True])
def test_prepared_role_is_frozen_at_allocation_not_current_settings(custody, tech_lead):
    config = Config(repo="owner/repo")
    config.tech_lead_review_agent = "agent:test" if tech_lead else None
    allocator = IssueRunAllocationService(FileSystemSessionOutput(), custody.ledger, custody.wc, configuration=config)
    recorded = custody.ledger.recorded_run(custody.run)
    run = allocator.allocate(IssueRunAllocation(custody.worktree, "coding-2", 42,
        recorded.session_key, "agent:test", "test", terminal_id="visible-worker"))
    config.tech_lead_review_agent = "agent:changed"
    receipt = custody.intake.submit(custody.ledger.submission_capability(run), command(completion()))
    candidate = custody.intake.prepare_receipt_for_issue(receipt, run, 42)
    expected = CompletionRunRole(42, TaskKind.TECH_LEAD if tech_lead else TaskKind.CODE, "agent:test")
    assert candidate.role == expected
    assert candidate.run.session_key.task is TaskKind.CODE
    assert candidate.run.branch_name == "feature"
    assert candidate.run.terminal_binding == RunTerminalBinding("visible-worker")
    reopened = SqliteIssueRunLedger(custody.state / "runs.sqlite")
    assert reopened.role_for_receipt(receipt.entry_id) == expected
    assert reopened.prepare_candidate(receipt.entry_id, reopened.recorded_run(run)) == candidate


@pytest.mark.parametrize("columns", [
    ("branch_name", "terminal_binding"),
    ("agent_label", "completion_task"),
    ("branch_name", "terminal_binding", "agent_label", "completion_task"),
])
def test_schema_join_preserves_known_fields_without_inventing_missing_ones(custody, columns):
    original = custody.ledger.recorded_run(custody.run)
    path = custody.state / "runs.sqlite"
    with sqlite3.connect(path) as conn:
        for column in columns:
            conn.execute(f"ALTER TABLE issue_runs DROP COLUMN {column}")
    reopened = SqliteIssueRunLedger(path)
    assert reopened.recorded_run(custody.run) == replace(original, **dict.fromkeys(columns))
    # Migrated physical column ordering must not change fresh insert bindings.
    next_record = replace(original, run=FileSystemSessionOutput().start_run(custody.worktree, "fresh"))
    reopened.record_run(42, next_record)
    assert SqliteIssueRunLedger(path).recorded_run(next_record.run) == next_record


@pytest.mark.parametrize("change", [
    {"branch_name": "different"},
    {"terminal_binding": RunTerminalBinding(None)},
    {"agent_label": "agent:different"},
    {"completion_task": TaskKind.TECH_LEAD},
])
def test_registration_retry_cannot_rebind_any_frozen_authority(custody, change):
    original = custody.ledger.recorded_run(custody.run)
    with pytest.raises(IssueRunEvidenceUnavailable, match="Conflicting ownership"):
        custody.ledger.record_run(42, replace(original, **change))
    assert custody.ledger.recorded_run(custody.run) == original


def test_unknown_role_refuses_before_processing_or_automatic_admission(custody):
    receipt = custody.intake.submit(custody.capability, command(completion()))
    with sqlite3.connect(custody.state / "runs.sqlite") as conn:
        conn.execute("UPDATE issue_runs SET completion_task=NULL")
    with pytest.raises(CompletionIntakeError, match="role is missing"):
        custody.intake.prepare_receipt_for_issue(receipt, custody.run, 42)
    assert custody.ledger.validation_for_receipt(receipt.entry_id) is None
    assert custody.ledger.pending_receipts()
    with pytest.raises(CompletionIntakeError, match="role is missing"):
        custody.lifecycle.terminate(42, "stop")
    assert custody.store.for_issue(42).dispositions == ()
    custody.pair.release.assert_not_called()


@pytest.mark.parametrize("task", [TaskKind.REVIEW, TaskKind.TECH_LEAD])
def test_historical_operator_role_cannot_become_an_agent_authority(task):
    with pytest.raises(CompletionIntakeError, match="role is missing or invalid"):
        CompletionRunRole(42, task, "operator:historical")
