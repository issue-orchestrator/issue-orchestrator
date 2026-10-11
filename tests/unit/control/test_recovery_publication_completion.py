"""Publication routes review and resolves before safe, restartable cleanup."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from issue_orchestrator.domain.human_block import BlockOutcome
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from issue_orchestrator.adapters.issue_disposition_gate import FileIssueDispositionMutationGate
from issue_orchestrator.control.actions import AddLabelAction, ActionResult
from issue_orchestrator.control.aggregate_recovery_block import AggregateRecoveryBlocks
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NO_OTHER_NEEDS_HUMAN_CAUSES
from issue_orchestrator.control.recovery_publication_cleanup import RecoveryPublicationCleanup
from issue_orchestrator.control.recovery_publication_completion import RecoveryPublicationCompletion
from issue_orchestrator.control.retry_review_routing import RetryReviewPolicy
from issue_orchestrator.control.staged_published_work_finalizer import StagedPublishedWorkFinalizer
from issue_orchestrator.control.validated_work_admission import RankedEvidenceAdmission
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.recovery_completion import RecoveryCompleted
from issue_orchestrator.domain.validated_work import ValidatedWorkState, FinalizationPhase, ReviewDisposition
from issue_orchestrator.execution.publication_workspace import EscrowPublicationWorkspaces
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from tests.unit.control.liveness_doubles import rate_limited
from tests.unit.control.test_recovery_publication_attempt import publication as publication, held
from tests.unit.control.test_retained_completion_preparation import retained as retained
from tests.unit.test_validated_work_preservation import custody as custody


class Labels:
    def __init__(self):
        self.labels = {"recovery-pending"}
        self.operations = []
        #: A typed host rate limit every label write reports instead.
        self.limit = None

    def read_issue_labels(self, issue_number):
        assert issue_number == 42
        return sorted(self.labels)

    def apply(self, action):
        assert action.issue_number == 42
        if self.limit is not None:
            return ActionResult.fail_limited(action, "API rate limit exceeded", self.limit)
        if isinstance(action, AddLabelAction):
            self.labels.add(action.label)
            self.operations.append(("add", action.label))
        else:
            self.labels.discard(action.label)
            self.operations.append(("remove", action.label))
        return ActionResult.ok(action)


@pytest.fixture
def completion(publication):
    rig = publication
    gate = FileIssueDispositionMutationGate(rig.custody.state)
    labels = Labels()
    aggregate = AggregateRecoveryBlocks(repo_slug="owner/repo", records=rig.store,
        admission=RankedEvidenceAdmission(rig.store, rig.custody.ledger),
        phases=rig.store,
        authority=rig.effects, gate=gate, labels=LabelManager(Config(repo="owner/repo")),
        reader=labels, applier=labels, human_block=NO_OTHER_NEEDS_HUMAN_CAUSES)
    finalizer = StagedPublishedWorkFinalizer(effects=rig.effects, phases=rig.store,
        recovery=aggregate, fresh_issue_reader=labels, action_applier=labels,
        review_policy=RetryReviewPolicy(code_review_agent_configured=True), routing_label="pr-pending")
    workspaces = EscrowPublicationWorkspaces(root=rig.custody.escrow.root.parent / "validated-work-publications", repository=rig.custody.repo,
        repo_slug="owner/repo", escrow=rig.custody.escrow, git=rig.custody.git, prepare=lambda _: None)
    holds = SimpleNamespace(calls=[], outcome=BlockOutcome.HELD)
    def hold_merge(pr_number):
        holds.calls.append((pr_number, tuple(labels.operations)))
        return holds.outcome
    def build_owner(assets):
        cleanup = RecoveryPublicationCleanup(store=rig.store, effects=rig.effects,
            blocks=aggregate, workspaces=assets, gate=gate)
        return RecoveryPublicationCompletion(store=rig.store, effects=rig.effects, finalizer=finalizer,
            verifier=rig.verifier, cleanup=cleanup, recovery_label="recovery-pending",
            hold_merge=hold_merge)
    return SimpleNamespace(rig=rig, owner=build_owner(workspaces), labels=labels, holds=holds,
        workspaces=workspaces, build_owner=build_owner, aggregate=aggregate, gate=gate)


def test_actual_publication_routes_review_before_releasing_recovery_and_keeps_escrow(completion):
    rig, owner = completion.rig, completion.owner
    state = OrchestratorState()
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        result = owner.complete(token, claim, rig.prepared, target, state, "Retained feature")
        assert isinstance(result, RecoveryCompleted)
        record = rig.store.record_for_id(claim.record_id)
        assert record.disposition.state is ValidatedWorkState.RECOVERED
        assert record.finalization_phase is FinalizationPhase.COMPLETE
        assert completion.labels.labels == {"pr-pending"}
        assert completion.labels.operations == [("add", "pr-pending"), ("remove", "recovery-pending")]
        assert len(state.discovered_reviews) == len(state.session_history) == 1
        assert state.discovered_reviews[0].pr_number == target.pr_number
        assert not rig.prepared.workspace.checkout.exists()
        assert rig.custody.escrow.verifies(record.current_evidence)
        assert rig.store.holds_claim(claim)


def test_an_agents_merge_hold_goes_on_the_published_pr_before_review_is_routed(completion):
    """#7678: recovery of a record whose agent asked a person to decide before
    the PR merges holds THAT PR's merge, before the publication resolves."""
    from dataclasses import replace

    rig, owner = completion.rig, completion.owner
    prepared = replace(rig.prepared, merge_hold_requested=True)
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, prepared, approved=rig.authority)
        result = owner.complete(token, claim, prepared, target, OrchestratorState(), "Retained feature")

    assert isinstance(result, RecoveryCompleted)
    assert completion.holds.calls == [(target.pr_number, ())]  # before any routing write


def test_a_merge_hold_that_does_not_commit_leaves_recovery_pending(completion):
    from dataclasses import replace

    rig, owner = completion.rig, completion.owner
    completion.holds.outcome = BlockOutcome.FAILED
    prepared = replace(rig.prepared, merge_hold_requested=True)
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, prepared, approved=rig.authority)
        result = owner.complete(token, claim, prepared, target, OrchestratorState(), "Retained feature")
        assert isinstance(result, RecoveryAttemptPending)
        assert "merge hold" in result.message
        assert rig.store.get(claim.record_id).state is not ValidatedWorkState.RECOVERED
    assert completion.labels.operations == []


def test_retry_after_cleanup_failure_replays_memory_without_publishing_again(completion):
    rig, owner = completion.rig, completion.owner
    class InterruptedCleanup:
        def release(self, admission):
            raise OSError("cleanup interrupted")
    interrupted = completion.build_owner(InterruptedCleanup())
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        with pytest.raises(OSError, match="cleanup interrupted"):
            interrupted.complete(token, claim, rig.prepared, target, OrchestratorState(), "Retained feature")
        assert rig.store.get(claim.record_id).state is ValidatedWorkState.RECOVERED
        attempts = rig.store.publish_attempts(claim.record_id)
        replacement_state = OrchestratorState()
        assert isinstance(owner.complete(token, claim, rig.prepared, target, replacement_state, "Retained feature"), RecoveryCompleted)
        assert len(replacement_state.discovered_reviews) == len(replacement_state.session_history) == 1
        assert rig.store.publish_attempts(claim.record_id) == attempts
        assert rig.remote.created == 1
        assert not rig.prepared.workspace.checkout.exists()


def test_cleanup_refuses_unresolved_publication_and_preserves_assets(completion):
    rig = completion.rig
    cleanup = RecoveryPublicationCleanup(store=rig.store, effects=rig.effects,
        blocks=completion.aggregate, workspaces=completion.workspaces, gate=completion.gate)
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        with pytest.raises(ValueError, match="requires durable publication resolution"):
            cleanup.release(token, claim, target)
        assert rig.prepared.workspace.checkout.exists()
        assert completion.labels.labels == {"recovery-pending"}


@pytest.mark.parametrize("change", ["pr", "review"])
def test_cleanup_requires_durable_pr_and_review_identity(completion, change):
    rig = completion.rig
    class InterruptedCleanup:
        def release(self, admission):
            raise OSError("cleanup interrupted")
    interrupted = completion.build_owner(InterruptedCleanup())
    cleanup = RecoveryPublicationCleanup(store=rig.store, effects=rig.effects,
        blocks=completion.aggregate, workspaces=completion.workspaces, gate=completion.gate)
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        with pytest.raises(OSError, match="cleanup interrupted"):
            interrupted.complete(token, claim, rig.prepared, target, OrchestratorState(), "Retained feature")
        wrong = (replace(target, pr_number=999, pr_url="https://example.invalid/owner/repo/pull/999")
            if change == "pr" else replace(target, review_disposition=ReviewDisposition.EXCHANGE_APPROVED))
        before = list(completion.labels.operations)
        with pytest.raises(ValueError, match="does not prove this publication"):
            cleanup.release(token, claim, wrong)
        assert rig.prepared.workspace.checkout.exists()
        assert completion.labels.operations == before


def test_a_rate_limited_routing_write_leaves_a_pending_result_with_its_reset(completion):
    """Finalization turns the limit into a transient outcome; completion must
    hand it to the drain's liveness owner rather than drop it (#7350)."""
    rig, owner = completion.rig, completion.owner
    completion.labels.limit = limit = rate_limited().rate_limit
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        result = owner.complete(token, claim, rig.prepared, target, OrchestratorState(), "Retained feature")
        assert isinstance(result, RecoveryAttemptPending)
        assert result.rate_limit == limit
        assert rig.store.get(claim.record_id).state is ValidatedWorkState.PUBLISHING
