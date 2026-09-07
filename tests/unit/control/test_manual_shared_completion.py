"""Manual recovery crosses the real completion policy owner with frozen custody."""

from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.manual_completion_preparation import ManualCompletionPreparation
from issue_orchestrator.control.manual_publication import ManualCompletionPublisher
from issue_orchestrator.domain.completion_processing import ProcessingResult
from issue_orchestrator.domain.exact_git import ExactPushOutcome
from issue_orchestrator.domain.manual_publication import PreparedManualPublication
from issue_orchestrator.domain.registered_completion import CompletionProcessingPolicy
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.validated_head_publication import BranchWriteOutcome, BranchWriteStatus, PrEnsureOutcome, PrEnsureStatus
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.validated_head_publication import ValidatedHeadExecutor
from tests.run_allocation_helpers import make_completion_processor
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.unit.control.test_manual_completion_preparation import rig as rig, custody as custody
from tests.unit.test_completion_evidence_intake import command
from tests.unit.test_completion_processor import (
    mock_git_adapter as mock_git_adapter, mock_label_adapter as mock_label_adapter,
    mock_pr_adapter as mock_pr_adapter,
)


@pytest.fixture
def shared(rig, mock_git_adapter, mock_label_adapter, mock_pr_adapter):
    custody, evidence, locators, _, _ = rig
    raw = json.loads(evidence.completion_bytes)
    raw["requested_actions"].append("remove_needs_rework_label")
    raw["pr_labels"] = ["feature"]
    receipt = custody.intake.submit(custody.capability, command(json.dumps(raw).encode(), "shared-owner"))
    locators = replace(locators, intake_receipt=receipt)
    config = Config(repo="owner/repo")
    mock_git_adapter.get_current_branch.return_value = "feature"
    mock_git_adapter.default_branch.return_value = "main"
    processor = make_completion_processor(
        label_adapter=mock_label_adapter, pr_adapter=mock_pr_adapter, git_adapter=mock_git_adapter,
        session_output=FileSystemSessionOutput(), agent_callback_endpoint=ready_callback_endpoint(),
        config=config, completion_intake=custody.intake,
    )
    owner = ManualCompletionPreparation(intake=custody.intake, completion=processor,
                                       working_copy=custody.wc, repo_slug="owner/repo")
    executor = Mock(spec=ValidatedHeadExecutor)
    return custody, locators, owner, executor, config


@pytest.mark.parametrize("existing_pr", [None, 99])
def test_new_and_existing_manual_prs_use_shared_policy_and_preserve_requested_effects(
    shared, mock_git_adapter, mock_label_adapter, mock_pr_adapter, existing_pr,
):
    custody, locators, owner, executor, config = shared
    locators = replace(locators, pr_number=existing_pr)
    prepared = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(prepared, PreparedManualPublication), prepared
    assert prepared.processing_policy == CompletionProcessingPolicy("agent:test", TaskKind.CODE)
    target = prepared.command.target_head_sha
    number = existing_pr or 42
    def push(command):
        # Once the processing invocation has selected CODE, a settings edit
        # during publication must not reclassify its remaining effects.
        config.tech_lead_review_agent = "agent:test"
        return BranchWriteOutcome(BranchWriteStatus.PUSHED, target, ExactPushOutcome.PUSHED, None, "Pushed")
    executor.push_validated_head.side_effect = push
    executor.ensure_pull_request.return_value = PrEnsureOutcome(
        PrEnsureStatus.ADOPTED if existing_pr else PrEnsureStatus.CREATED,
        number, f"https://github.com/owner/repo/pull/{number}", target, None, "Published")
    (custody.worktree / ".issue-orchestrator/completion.json").write_text('{"outcome":"blocked"}')
    result = ManualCompletionPublisher(owner, executor).publish(locators, "Feature", lambda: True)
    assert result.processing.success, result.processing.errors
    assert result.processing.require_processing_policy() == prepared.processing_policy
    assert result.processing.intake_receipt == locators.intake_receipt
    mock_label_adapter.add_label.assert_called_once_with(number, "feature")
    mock_label_adapter.remove_label.assert_called_once_with(existing_pr or 42, "needs-rework")
    mock_git_adapter.push.assert_not_called()
    mock_pr_adapter.create_pr.assert_not_called()
    assert executor.ensure_pull_request.call_args.args[0].target_head_sha == target


def test_shared_source_policy_refusal_prevents_every_remote_publication_step(shared, mock_git_adapter):
    _, locators, owner, executor, _ = shared
    mock_git_adapter.get_current_branch.return_value = "main"
    result = ManualCompletionPublisher(owner, executor).publish(locators, "Feature", lambda: True)
    assert isinstance(result.processing, ProcessingResult)
    assert not result.processing.success
    assert result.processing.intake_receipt == locators.intake_receipt
    assert result.processing.require_processing_policy() == CompletionProcessingPolicy("agent:test", TaskKind.CODE)
    executor.push_validated_head.assert_not_called()
    executor.ensure_pull_request.assert_not_called()
