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
from issue_orchestrator.domain.registered_completion import CompletionProcessingPolicy
from issue_orchestrator.domain.session_key import TaskKind
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
        record, custody.run.session_name, CompletionProcessingPolicy("agent:test", TaskKind.CODE), "feature", str(evidence.entry.normalized_path),
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


@pytest.fixture
def execution_rig(rig):
    from issue_orchestrator.control.manual_publication import ManualCompletionPublisher
    from issue_orchestrator.execution.git_validated_head_executor import GitValidatedHeadExecutor
    from issue_orchestrator.domain.validated_work import PublishValidatedHeadStatus
    from tests.unit.test_git_validated_head_executor import Remote

    custody, evidence, locators, shared, owner = rig
    remote_path = custody.repo.parent / "remote.git"
    custody.git.run(custody.repo, ["clone", "--bare", str(custody.repo), str(remote_path)])
    custody.git.run(custody.repo, ["remote", "add", "origin", str(remote_path)])
    base = custody.git.run(custody.worktree, ["rev-parse", "HEAD^"]).stdout.strip()
    custody.git.run(remote_path, ["update-ref", "refs/heads/feature", base])
    custody.remote = remote_path
    remote = Remote(custody)
    shared.settle_manual_publication.side_effect = lambda prepared, outcome: ProcessingResult(
        outcome.status in {PublishValidatedHeadStatus.PUBLISHED, PublishValidatedHeadStatus.ALREADY_AT_TARGET},
        outcome.message, pr_url=outcome.pr_url, intake_receipt=prepared.receipt)
    worker = ManualCompletionPublisher(owner, GitValidatedHeadExecutor(custody.wc, remote))
    return custody, evidence, locators, shared, owner, remote, worker, base


@pytest.mark.parametrize("pr_state", ["existing", "missing", "accepted-response-lost"])
def test_manual_worker_moves_remote_to_validated_head_before_pr_settlement(execution_rig, pr_state):
    from issue_orchestrator.domain.validated_work import PublishValidatedHeadStatus

    custody, evidence, locators, shared, owner, remote, worker, base = execution_rig
    prepared = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(prepared, PreparedManualPublication)
    assert remote.read_branch(prepared.command) == base
    assert base != prepared.command.target_head_sha
    if pr_state == "existing":
        pr = remote.add_pr(prepared.command, body="Older explicitly recorded PR")
        locators = replace(locators, pr_number=pr.number)
    remote.lost_create = pr_state == "accepted-response-lost"
    result = worker.publish(locators, "Feature", lambda: True)
    assert result.processing.success
    assert result.publication is not None
    assert result.publication.status is PublishValidatedHeadStatus.PUBLISHED
    assert result.publication.pr_head_sha == evidence.validation.head_sha
    assert remote.read_branch(prepared.command) == evidence.validation.head_sha
    assert custody.git.head_sha(custody.worktree) == evidence.validation.head_sha
    assert remote.created == (0 if pr_state == "existing" else 1)
    settled, observed = shared.settle_manual_publication.call_args.args
    assert settled.command.target_head_sha == observed.pr_head_sha == evidence.validation.head_sha


@pytest.mark.parametrize("mutation", ["unseen-before-read", "advance-after-read", "diverge-after-read"])
def test_manual_exact_lease_refuses_remote_mutation(execution_rig, mutation):
    from issue_orchestrator.domain.validated_work import PublishValidatedHeadStatus

    custody, evidence, locators, _, owner, remote, worker, base = execution_rig
    prepared = owner.prepare_manual_publication(locators, "Feature")
    assert isinstance(prepared, PreparedManualPublication)
    custody.git.run(custody.remote, ["config", "user.name", "Remote mutation test"])
    custody.git.run(custody.remote, ["config", "user.email", "remote@example.invalid"])
    parent = evidence.validation.head_sha if mutation == "advance-after-read" else base
    tree = custody.git.run(custody.remote, ["rev-parse", f"{parent}^{{tree}}"]).stdout.strip()
    third = custody.git.run(custody.remote, ["commit-tree", tree, "-p", parent, "-m", "Remote concurrent change"]).stdout.strip()
    def mutate():
        custody.git.run(custody.remote, ["update-ref", "refs/heads/feature", third])
    if mutation == "unseen-before-read":
        mutate()
    else:
        remote.on_branch_read = mutate
    result = worker.publish(locators, "Feature", lambda: True)
    assert not result.processing.success
    assert result.publication is not None
    if mutation == "unseen-before-read":
        from issue_orchestrator.domain.validated_work import ValidatedWorkFailure
        assert result.publication.status is PublishValidatedHeadStatus.REJECTED
        assert result.publication.failure is ValidatedWorkFailure.REMOTE_BASELINE_UNPROVEN
        assert result.publication.push_outcome is None
    else:
        assert result.publication.status is PublishValidatedHeadStatus.DIVERGED
    assert remote.read_branch(prepared.command) == third
    assert remote.created == 0
    assert custody.git.head_sha(custody.worktree) == evidence.validation.head_sha
