"""The worker honors the manual owner's lifetime and keeps partial effects."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.manual_publication import ManualCompletionPublisher
from issue_orchestrator.domain.completion_intake import CompletionIntakeReceipt
from issue_orchestrator.domain.completion_processing import ProcessingResult
from issue_orchestrator.domain.exact_git import ExactPushOutcome
from issue_orchestrator.domain.manual_publication import PreparedManualPublication
from issue_orchestrator.domain.models import CompletionOutcome, CompletionRecord
from issue_orchestrator.domain.session_run import RunContainedFile
from issue_orchestrator.domain.publish_retry import PublishRetryLocators
from issue_orchestrator.domain.registered_completion import CompletionProcessingPolicy
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.validated_head_publication import (
    BranchWriteOutcome, BranchWriteStatus, PrEnsureOutcome, PrEnsureStatus,
    PullRequestAttribution,
    PublicationContent, PublishValidatedHeadCommand, RemoteHeadExpectation,
    SupersededStage,
)
from issue_orchestrator.domain.validated_work import PublishValidatedHeadStatus, ValidatedWorkFailure
from issue_orchestrator.ports.manual_publication import ManualPublicationPreparation
from issue_orchestrator.ports.validated_head_publication import ValidatedHeadExecutor
from tests.unit.session_run_helpers import make_session_run_assets


@pytest.fixture
def rig(tmp_path):
    receipt = CompletionIntakeReceipt("a" * 64, "b" * 64)
    run = make_session_run_assets(tmp_path)
    locators = PublishRetryLocators(1, "Title", "code:1", str(tmp_path), "feature",
                                   "completion.json", run, intake_receipt=receipt)
    command = PublishValidatedHeadCommand(1, "owner/repo", "feature", "c" * 40,
        RemoteHeadExpectation.UNCONSTRAINED, None, tmp_path, None, "main",
        PublicationContent("Title", "Body", True))
    record = CompletionRecord("session", "2026-09-07T00:00:00Z", CompletionOutcome.COMPLETED,
                              "Summary", [])
    prepared = PreparedManualPublication(command, receipt, run, RunContainedFile(run.run_dir, run.run_dir / "owned.json"), record, "Title", CompletionProcessingPolicy("agent:coder", TaskKind.CODE), 1,
                                         (), (), None, None, False, False)
    preparation = Mock(spec=ManualPublicationPreparation)
    preparation.prepare_manual_publication.return_value = prepared
    preparation.settle_manual_publication.return_value = ProcessingResult(True, "Published", pr_url="https://github.com/owner/repo/pull/2")
    executor = Mock(spec=ValidatedHeadExecutor)
    executor.push_validated_head.return_value = BranchWriteOutcome(
        BranchWriteStatus.PUSHED, command.target_head_sha, ExactPushOutcome.PUSHED, None, "Pushed")
    executor.ensure_pull_request.return_value = PrEnsureOutcome(
        PrEnsureStatus.CREATED, 2, "https://github.com/owner/repo/pull/2",
        command.target_head_sha, None, "Created", PullRequestAttribution.CREATED)
    return locators, prepared, preparation, executor


@pytest.mark.parametrize("checks,stage,pushed", [
    ([False, False], SupersededStage.BEFORE_BRANCH_WRITE, False),
    ([True, False, False], SupersededStage.BETWEEN_STEPS, True),
])
def test_abandonment_uses_actual_owner_checks(rig, checks, stage, pushed):
    locators, prepared, preparation, executor = rig
    current = iter(checks)
    result = ManualCompletionPublisher(preparation, executor).publish(locators, "Title", lambda: next(current))
    assert result.publication is not None
    assert result.publication.status is PublishValidatedHeadStatus.SUPERSEDED
    assert result.publication.superseded_stage is stage
    assert result.publication.push_outcome is (ExactPushOutcome.PUSHED if pushed else None)
    assert executor.push_validated_head.call_count == int(pushed)
    executor.ensure_pull_request.assert_not_called()
    preparation.settle_manual_publication.assert_not_called()
    assert not result.processing.success
    assert result.processing.intake_receipt == prepared.receipt
    assert result.processing.require_processing_policy() == prepared.processing_policy


def test_late_pr_result_keeps_metadata_for_tombstone_owner(rig):
    locators, _, preparation, executor = rig
    current = iter([True, True, False])
    result = ManualCompletionPublisher(preparation, executor).publish(locators, "Title", lambda: next(current))
    assert result.publication is not None
    assert result.publication.status is PublishValidatedHeadStatus.PUBLISHED
    assert result.publication.pr_number == 2
    assert result.processing.pr_url == result.publication.pr_url
    assert not result.processing.success
    preparation.settle_manual_publication.assert_not_called()


def test_partial_pr_failure_reaches_settlement_without_losing_branch_fact(rig):
    locators, prepared, preparation, executor = rig
    executor.ensure_pull_request.return_value = replace(executor.ensure_pull_request.return_value,
        status=PrEnsureStatus.TRANSIENT_FAILURE, failure=ValidatedWorkFailure.REMOTE_UNREADABLE,
        message="Accepted create, final remote observation unavailable")
    preparation.settle_manual_publication.return_value = ProcessingResult(False, "Remote unreadable", pr_url="https://github.com/owner/repo/pull/2")
    result = ManualCompletionPublisher(preparation, executor).publish(locators, "Title", lambda: True)
    assert result.publication is not None
    assert result.publication.status is PublishValidatedHeadStatus.TRANSIENT_FAILURE
    assert result.publication.pr_number == 2
    assert result.publication.push_outcome is ExactPushOutcome.PUSHED
    preparation.settle_manual_publication.assert_called_once_with(prepared, result.publication)


def test_preparation_refusal_produces_no_remote_effects(rig):
    locators, _, preparation, executor = rig
    refusal = ProcessingResult(False, "Wrong receipt/run")
    preparation.prepare_manual_publication.return_value = refusal
    result = ManualCompletionPublisher(preparation, executor).publish(locators, "Title", lambda: True)
    assert result.processing is refusal
    assert result.publication is None
    executor.push_validated_head.assert_not_called()
    executor.ensure_pull_request.assert_not_called()


def test_terminal_success_requires_real_publication(rig):
    from issue_orchestrator.domain.manual_publication import ManualPublicationResult

    with pytest.raises(ValueError, match="successful exact publication"):
        ManualPublicationResult(ProcessingResult(True, "No work was published"), None)


def test_branch_refusal_never_ensures_pr(rig):
    locators, prepared, preparation, executor = rig
    executor.push_validated_head.return_value = BranchWriteOutcome(
        BranchWriteStatus.DIVERGED, "d" * 40, None, ValidatedWorkFailure.REMOTE_DIVERGED, "Diverged")
    preparation.settle_manual_publication.return_value = ProcessingResult(False, "Diverged")
    result = ManualCompletionPublisher(preparation, executor).publish(locators, "Title", lambda: True)
    assert result.publication is not None
    assert result.publication.status is PublishValidatedHeadStatus.DIVERGED
    executor.ensure_pull_request.assert_not_called()
    preparation.settle_manual_publication.assert_called_once_with(prepared, result.publication)


def test_settlement_exception_preserves_accepted_pr_for_owner(rig):
    locators, _, preparation, executor = rig
    preparation.settle_manual_publication.side_effect = RuntimeError("label write failed")
    result = ManualCompletionPublisher(preparation, executor).publish(locators, "Title", lambda: True)
    assert result.publication is not None
    assert result.publication.pr_number == 2
    assert result.publication.push_outcome is ExactPushOutcome.PUSHED
    assert result.processing.pr_url == result.publication.pr_url
    assert not result.processing.success
    assert "label write failed" in result.processing.message
