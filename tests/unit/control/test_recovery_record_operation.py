"""Complete synchronous record ownership against real custody, Git and SQLite."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.claimed_recovery_preparation import ClaimedRecoveryPreparation
from issue_orchestrator.control.recovery_record_operation import RecoveryRecordOperation
from issue_orchestrator.control.review_exchange_lifecycle import OtherRuntimeActivity
from issue_orchestrator.domain.completion_intake import CompletionIntakeError
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from issue_orchestrator.domain.recovery_completion import RecoveryCompleted
from issue_orchestrator.domain.recovery_entry import RecoveryIssue, RecoveryIssueState, RecoveryRecordRequest
from issue_orchestrator.domain.validated_work import ValidatedWorkFailure, ValidatedWorkState
from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy
from issue_orchestrator.ports.recovery_issue_reader import RecoveryIssueReadError
from tests.unit.control.test_recovery_publication_completion import completion as completion
from tests.unit.control.test_recovery_publication_attempt import publication as publication
from tests.unit.control.test_retained_completion_preparation import retained as retained
from tests.unit.test_validated_work_preservation import custody as custody


class Issues:
    def __init__(self):
        self.issue = RecoveryIssue("owner/repo", 42, "Recovered feature", RecoveryIssueState.OPEN, ("recovery-pending",))
        self.unreadable = False
        self.reads = 0

    def read(self, repo_slug, issue_number):
        assert (repo_slug, issue_number) == ("owner/repo", 42)
        self.reads += 1
        if self.unreadable:
            raise RecoveryIssueReadError("offline")
        return self.issue


def build_operation(completion, *, workspaces=None):
    rig = completion.rig
    for owner, method in [(rig.custody.pair, "has_active_pair"),
                          (rig.custody.jobs, "has_matching"), (rig.custody.retry, "has_active_retry")]:
        getattr(owner, method).return_value = False
    issues = Issues()
    preparation = ClaimedRecoveryPreparation(repo_slug="owner/repo", store=rig.store,
        effects=rig.effects, issues=issues, runtime=OtherRuntimeActivity(rig.custody.lifecycle.core),
        gate=completion.gate, workspaces=workspaces or completion.workspaces,
        preparation=rig.preparation,
        pause_label="io:needs-reconcile")
    owner = RecoveryRecordOperation(execution=rig.execution, store=rig.store, preparation=preparation,
        publication=rig.worker, completion=completion.owner)
    request = RecoveryRecordRequest(rig.authority.record_id, rig.authority.evidence_id, rig.authority)
    return SimpleNamespace(rig=rig, owner=owner, request=request, issues=issues)


@pytest.fixture
def operation(completion):
    return build_operation(completion)


def assert_retained_without_publication(op):
    assert op.rig.store.publish_attempts(op.request.record_id) == ()
    assert op.rig.store.owner_of(op.request.record_id) is None
    assert op.rig.remote.created == 0
    assert op.rig.prepared.workspace.checkout.exists()


def test_operation_owns_claim_through_publication_and_releases_after_cleanup(operation):
    op = operation
    state = OrchestratorState()
    result = op.owner.run(op.request, state)
    assert isinstance(result, RecoveryCompleted)
    assert op.rig.store.owner_of(op.request.record_id) is None
    assert op.rig.store.get(op.request.record_id).state is ValidatedWorkState.RECOVERED
    assert len(state.discovered_reviews) == 1
    assert not op.rig.prepared.workspace.checkout.exists()
    assert op.issues.reads == 1


@pytest.mark.parametrize("kind", ["closed", "paused", "unreadable"])
def test_issue_refusal_releases_claim_without_publication(operation, kind):
    op = operation
    if kind == "closed":
        op.issues.issue = replace(op.issues.issue, state=RecoveryIssueState.CLOSED)
    elif kind == "paused":
        op.issues.issue = replace(op.issues.issue, labels=("io:needs-reconcile",))
    else:
        op.issues.unreadable = True
    assert isinstance(op.owner.run(op.request, OrchestratorState()), RecoveryAttemptPending)
    assert_retained_without_publication(op)


@pytest.mark.parametrize("unverifiable", [False, True])
def test_other_runtime_activity_blocks_publication(operation, unverifiable):
    op = operation
    op.rig.custody.retry.has_active_retry.return_value = True
    if unverifiable:
        op.rig.custody.retry.has_active_retry.side_effect = OSError("unknown runtime")
    result = op.owner.run(op.request, OrchestratorState())
    assert isinstance(result, RecoveryAttemptPending)
    assert result.failure is ValidatedWorkFailure.RUNTIME_ACTIVE
    assert_retained_without_publication(op)


def test_workspace_setup_failure_becomes_retained_recovery_failure(completion):
    workspaces = Mock(prepare=Mock(side_effect=CompletionIntakeError(
        "publication workspace setup failed")))
    op = build_operation(completion, workspaces=workspaces)
    result = op.owner.run(op.request, OrchestratorState())
    assert isinstance(result, RecoveryAttemptPending)
    assert result.failure is ValidatedWorkFailure.WORKSPACE_INTEGRITY
    assert "workspace setup failed" in result.message
    assert_retained_without_publication(op)


def test_stale_or_missing_approval_never_claims_or_reads_remote_issue(operation):
    op = operation
    for request in [replace(op.request, approved=None),
                    replace(op.request, approved=replace(op.request.approved, observation_revision=99))]:
        assert isinstance(op.owner.run(request, OrchestratorState()), RecoveryAttemptPending)
    assert op.issues.reads == 0
    assert_retained_without_publication(op)


def test_busy_execution_never_enters_other_record_capabilities(operation):
    op = operation
    lease = op.rig.execution.try_enter(op.request.record_id)
    assert not isinstance(lease, RecordExecutionBusy)
    with lease:
        result = op.owner.run(op.request, OrchestratorState())
        assert isinstance(result, RecoveryAttemptPending)
        assert op.issues.reads == 0
    assert_retained_without_publication(op)
