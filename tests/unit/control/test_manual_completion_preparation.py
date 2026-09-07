"""Real custody and source checks before the shared completion policy owner."""

from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_preparation import PreparedActionPlan, PreparedCompletion, PreparedPullRequest
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.manual_completion_preparation import ManualCompletionPreparation
from issue_orchestrator.control.review_publish_pipeline import PublishPipelinePlan
from issue_orchestrator.domain.completion_processing import ProcessingResult
from issue_orchestrator.domain.manual_publication import PreparedManualPublication
from issue_orchestrator.domain.models import CompletionRecord, RequestedAction
from issue_orchestrator.domain.publish_retry import PublishRetryLocators
from tests.unit.test_completion_evidence_intake import command, completion
from tests.unit.test_validated_work_preservation import custody as custody, advance


@pytest.fixture
def rig(custody):
    (custody.worktree / ".gitignore").write_text(".issue-orchestrator/\n")
    custody.git.run(custody.worktree, ["add", ".gitignore"])
    custody.git.run(custody.worktree, ["commit", "-m", "Ignore run artifacts"])
    raw = json.loads(completion())
    raw["requested_actions"].append("create_pr")
    record = CompletionRecord.from_dict(raw)
    receipt = custody.intake.submit(custody.capability, command(json.dumps(raw).encode()))
    evidence = custody.intake.prepare_receipt_for_issue(receipt, custody.run, 42)
    locators = PublishRetryLocators(42, "Feature", evidence.run.session_key.stable_id(),
        str(custody.worktree), "feature", "untrusted-agent.json", custody.run,
        agent_label="agent:forged", intake_receipt=receipt)
    shared = Mock(spec=CompletionProcessor)
    shared.prepare_completion.return_value = PreparedCompletion(
        record, custody.run.session_name, "agent:test", "feature", str(evidence.entry.normalized_path),
        PreparedActionPlan(PublishPipelinePlan(tuple(record.requested_actions), False), None, None, False, False))
    shared.prepare_pull_request.return_value = PreparedPullRequest("#42: Feature", "Prepared body", "main", None, None)
    owner = ManualCompletionPreparation(intake=custody.intake, completion=shared,
                                        working_copy=custody.wc, repo_slug="owner/repo")
    return custody, evidence, locators, shared, owner


def test_manual_command_uses_owned_bytes_and_immutable_attestation(rig):
    custody, evidence, locators, shared, owner = rig
    (custody.worktree / ".issue-orchestrator" / "completion.json").write_text('{"outcome":"blocked"}')
    result = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(result, PreparedManualPublication)
    assert result.command.target_head_sha == evidence.validation.head_sha
    assert result.command.branch_name == evidence.run.branch_name
    assert result.agent_label == "agent:test"
    assert result.command.content.body == "Prepared body"
    supplied = shared.prepare_completion.call_args.kwargs
    assert supplied["prepared_evidence"].completion_bytes == evidence.completion_bytes
    assert supplied["intake_receipt"] == locators.intake_receipt
    assert supplied["agent_label"] is None
    assert supplied["completion_path"] is None
    assert result.remaining_actions == ()


@pytest.mark.parametrize("damage", ["receipt", "run", "issue", "branch", "session", "workspace", "missing-receipt"])
def test_wrong_locators_never_reach_completion_policy(rig, damage):
    custody, _, locators, shared, owner = rig
    if damage == "receipt":
        locators = replace(locators, intake_receipt=replace(locators.intake_receipt, content_sha256="0" * 64))
    elif damage == "run":
        locators = replace(locators, run_assets=replace(locators.run_assets,
            manifest=replace(locators.run_assets.manifest, path=locators.run_assets.run_dir / "other.json")))
    elif damage == "issue":
        locators = replace(locators, issue_number=43)
    elif damage == "branch":
        locators = replace(locators, branch_name="other")
    elif damage == "session":
        locators = replace(locators, session_key="code:43")
    elif damage == "workspace":
        locators = replace(locators, worktree_path=str(custody.repo))
    else:
        locators = replace(locators, intake_receipt=None)
    result = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(result, ProcessingResult) and not result.success
    shared.prepare_completion.assert_not_called()
    shared.prepare_pull_request.assert_not_called()


@pytest.mark.parametrize("change", ["dirty", "new-head", "detached"])
def test_unvalidated_source_refuses_without_modifying_it(rig, change):
    custody, evidence, locators, shared, owner = rig
    if change == "dirty":
        (custody.worktree / "content").write_text("uncommitted work")
    elif change == "new-head":
        advance(custody)
    else:
        custody.git.run(custody.worktree, ["checkout", "--detach"])
    before = custody.git.head_sha(custody.worktree)
    result = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(result, ProcessingResult) and not result.success
    assert custody.git.head_sha(custody.worktree) == before
    assert custody.ledger.validation_for_receipt(evidence.entry.entry_id).head_sha == evidence.validation.head_sha
    shared.prepare_completion.assert_not_called()


def test_review_that_changes_head_cannot_replace_validated_target(rig):
    custody, _, locators, shared, owner = rig
    prepared = shared.prepare_completion.return_value
    def review(*args, **kwargs):
        advance(custody)
        return prepared
    shared.prepare_completion.side_effect = review
    result = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(result, ProcessingResult) and not result.success
    assert "changed the validated source" in result.message


@pytest.mark.parametrize("phase", ["review", "stack"])
def test_shared_policy_refusal_never_builds_a_publication_command(rig, phase):
    _, _, locators, shared, owner = rig
    if phase == "review":
        shared.prepare_completion.return_value = ProcessingResult(False, "review refused")
    else:
        shared.prepare_pull_request.return_value = None
    result = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(result, ProcessingResult) and not result.success
    if phase == "review":
        shared.prepare_pull_request.assert_not_called()
