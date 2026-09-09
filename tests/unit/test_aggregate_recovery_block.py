"""Sibling blocking through real durable publication and restart boundaries."""

from dataclasses import replace

import pytest

from issue_orchestrator.control.needs_human_block import (
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanCause,
    ValidatedWorkBlockSource,
)
from issue_orchestrator.domain.issue_disposition_gate import IssueDispositionGateStatus
from issue_orchestrator.domain.published_work_finalization import FinalizationStatus
from issue_orchestrator.domain.recovery_block import RecoveryBlockReconcileStatus
from issue_orchestrator.domain.recovery_block import RecoveryAdmissionDeferred
from issue_orchestrator.domain.validated_work import (
    FinalizationPhase as Phase,
    ValidatedWorkState as State,
    ValidatedWorkFailure as Failure,
)
from tests.unit.aggregate_recovery_support import (
    AggregateRig,
    assert_admission_busy_in_child,
    assert_human_acquisition_busy_in_child,
)
from tests.unit.staged_finalization_support import Crash
from tests.unit.validated_work_support import capture


def test_real_aggregate_finalizes_and_preserves_later_operator_block(tmp_path):
    rig = AggregateRig(tmp_path)
    rig.remote.labels.add("needs-human")
    result = rig.base.invoke()
    assert result.status is FinalizationStatus.FINALIZED
    assert result.phase_reached is Phase.RECOVERY_CLEARED
    assert set(result.labels_removed) == {"recovery-pending", "blocked-failed"}
    assert rig.remote.labels == {"pr-pending", "needs-human"}
    assert rig.base.store.get(rig.base.claim.record_id).state is State.PUBLISHING
    rig.reopen()
    assert (
        rig.aggregate.reconcile_issue_block(6914).status
        is RecoveryBlockReconcileStatus.RECONCILED
    )
    assert rig.remote.labels == {"pr-pending", "needs-human"}


@pytest.mark.parametrize(
    "state,failure",
    [
        (State.QUEUED, None),
        (State.PARKED, None),
        (State.FAILED, Failure.ARTIFACT_MISSING),
    ],
)
def test_sibling_keeps_recovery_and_captured_blocks_after_own_release(
    tmp_path, state, failure
):
    rig = AggregateRig(tmp_path)
    sibling = capture(branch="sibling", state=state, failure=failure)
    rig.base.store.admit(sibling)
    result = rig.base.invoke()
    assert result.status is FinalizationStatus.FINALIZED
    assert result.labels_removed == ()
    assert {"recovery-pending", "blocked-failed", "pr-pending"} <= rig.remote.labels
    assert ("needs-human" in rig.remote.labels) == (state is State.FAILED)
    rig.reopen()
    rig.aggregate.reconcile_issue_block(6914)
    assert "recovery-pending" in rig.remote.labels
    assert (
        rig.base.store.finalization_phase(rig.base.claim.record_id)
        is Phase.RECOVERY_CLEARED
    )


def test_failed_sibling_source_survives_published_records_withdrawal(tmp_path):
    rig = AggregateRig(tmp_path)
    own_source = HumanBlockRequest(
        6914,
        NeedsHumanCause.VALIDATED_WORK_DISPOSITION,
        "earlier failure",
        ValidatedWorkBlockSource(rig.base.claim.record_id),
    )
    rig.human.acquire(own_source)
    sibling = capture(
        branch="failed-sibling", state=State.FAILED, failure=Failure.ARTIFACT_MISSING
    )
    rig.base.store.admit(sibling)
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    causes = rig.causes.needs_human_causes(6914)
    assert own_source.cause_key not in causes
    assert causes == frozenset(
        {f"validated_work_disposition:{sibling.evidence.record_id}"}
    )
    assert "needs-human" in rig.remote.labels
    rig.remote.labels.remove("needs-human")
    rig.reopen()
    rig.aggregate.reconcile_issue_block(6914)
    assert "needs-human" in rig.remote.labels
    assert rig.causes.needs_human_causes(6914) == causes


def test_busy_gate_refuses_release_and_reconcile_without_shedding(tmp_path):
    rig = AggregateRig(tmp_path)
    with rig.gate.try_acquire("owner/repo", 6914) as acquired:
        assert acquired is IssueDispositionGateStatus.ACQUIRED
        assert (
            rig.aggregate.reconcile_issue_block(6914).status
            is RecoveryBlockReconcileStatus.BUSY
        )
        assert rig.base.invoke().status is FinalizationStatus.TRANSIENT
        assert not any(action == "remove" for action, _ in rig.remote.actions)
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED


@pytest.mark.parametrize("label", ["blocked-failed", "recovery-pending"])
def test_lost_label_write_reconciles_durable_interests_then_finishes(tmp_path, label):
    rig = AggregateRig(tmp_path)
    rig.remote.crash_after = label
    with pytest.raises(Crash):
        rig.base.invoke()
    assert (
        rig.base.store.finalization_phase(rig.base.claim.record_id)
        is Phase.REVIEW_ROUTED
    )
    rig.remote.crash_after = ""
    rig.reopen()
    rig.aggregate.reconcile_issue_block(6914)
    assert "recovery-pending" in rig.remote.labels
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    assert rig.remote.labels == {"pr-pending"}


def test_failed_write_keeps_phase_retryable_and_never_reports_release(tmp_path):
    rig = AggregateRig(tmp_path)
    rig.remote.fail_remove = "blocked-failed"
    result = rig.base.invoke()
    assert result.status is FinalizationStatus.TRANSIENT
    assert result.phase_reached is Phase.REVIEW_ROUTED
    assert "recovery-pending" in rig.remote.labels
    rig.remote.fail_remove = ""
    # A remote exception cannot prove the write did not commit. Preserve a
    # currently present label until its owner resolves the ambiguous generation.
    assert rig.base.invoke().status is FinalizationStatus.TRANSIENT
    assert "blocked-failed" in rig.remote.labels
    rig.remote.labels.remove("blocked-failed")
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED


def test_caller_cannot_invent_captured_blockers_to_release(tmp_path):
    rig = AggregateRig(tmp_path)
    result = rig.base.invoke(
        observed_blocking_labels=("blocked-failed", "publish-failed")
    )
    assert result.status is FinalizationStatus.TRANSIENT
    assert "differs from retained" in result.message
    assert not any(action == "remove" for action, _ in rig.remote.actions)


def test_lost_claim_inside_shared_block_read_prevents_later_source_or_label_write(
    tmp_path,
):
    rig = AggregateRig(tmp_path)
    own_source = HumanBlockRequest(
        6914,
        NeedsHumanCause.VALIDATED_WORK_DISPOSITION,
        "old failure",
        ValidatedWorkBlockSource(rig.base.claim.record_id),
    )
    rig.human.acquire(own_source)
    # Establish the durable routing checkpoint so the first read is the
    # shared-block release. Every inner boundary must authenticate again.
    rig.remote.labels.add("pr-pending")
    assert rig.base.store.record_finalization_phase(
        rig.base.claim,
        phase=Phase.REVIEW_ROUTED,
        recorded_at="2026-09-06T13:00:00+00:00",
    )
    reads = 0

    def lose_during_shared_read():
        nonlocal reads
        reads += 1
        if reads == 1:
            assert rig.base.store.relinquish_claim(rig.base.claim)

    rig.remote.after_read = lose_during_shared_read
    result = rig.base.invoke()
    assert result.status is FinalizationStatus.TRANSIENT
    assert own_source.cause_key in rig.causes.needs_human_causes(6914)
    assert "needs-human" in rig.remote.labels
    assert not any(action == "remove" for action, _ in rig.remote.actions)


def test_snapshot_rejects_cross_repository_read(tmp_path):
    rig = AggregateRig(tmp_path)
    with pytest.raises(ValueError, match="another repository"):
        rig.base.store.recovery_block_snapshot("someone/else", 6914)


def test_acknowledged_cleanup_never_removes_later_operator_failure_labels(tmp_path):
    rig = AggregateRig(tmp_path)
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    snapshot = rig.base.store.recovery_block_snapshot("owner/repo", 6914)
    assert snapshot.interests[0].cleanup_acknowledged
    rig.remote.labels.update({"blocked-failed", "publish-failed"})
    rig.reopen()
    result = rig.aggregate.reconcile_issue_block(6914)
    assert result.status is RecoveryBlockReconcileStatus.RECONCILED
    assert {"blocked-failed", "publish-failed"} <= rig.remote.labels
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    assert {"blocked-failed", "publish-failed"} <= rig.remote.labels


def test_lost_cleanup_ack_is_recovered_before_releasing_aggregate_label(tmp_path):
    rig = AggregateRig(tmp_path)

    class LostAcknowledgement:
        def recovery_block_snapshot(self, repo_slug, issue_number):
            return rig.base.store.recovery_block_snapshot(repo_slug, issue_number)

        def begin_block_label_cleanup(self, keys, label):
            return rig.base.store.begin_block_label_cleanup(keys, label)

        def acknowledge_block_cleanup(self, keys):
            assert rig.base.store.acknowledge_block_cleanup(keys)
            raise Crash()

    rig.compose(records=LostAcknowledgement())
    with pytest.raises(Crash):
        rig.base.invoke()
    assert "recovery-pending" in rig.remote.labels
    assert "blocked-failed" not in rig.remote.labels
    rig.remote.labels.add("blocked-failed")  # A later operator write.
    rig.reopen()
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    assert "recovery-pending" not in rig.remote.labels
    assert "blocked-failed" in rig.remote.labels


def test_cleanup_acknowledgement_refuses_unrouted_or_changed_generation_atomically(
    tmp_path,
):
    rig = AggregateRig(tmp_path)
    key = (
        rig.base.store.recovery_block_snapshot("owner/repo", 6914)
        .interests[0]
        .cleanup_key
    )
    assert not rig.base.store.acknowledge_block_cleanup((key,))
    assert rig.base.store.record_finalization_phase(
        rig.base.claim,
        phase=Phase.REVIEW_ROUTED,
        recorded_at="2026-09-06T13:00:00+00:00",
    )
    assert not rig.base.store.acknowledge_block_cleanup(
        (key, replace(key, attempt_no=key.attempt_no + 1))
    )
    assert (
        not rig.base.store.recovery_block_snapshot("owner/repo", 6914)
        .interests[0]
        .cleanup_acknowledged
    )


@pytest.mark.parametrize("cross_process", [False, True])
def test_sibling_admission_cannot_enter_last_holder_removal_and_reasserts_before_return(
    tmp_path,
    cross_process,
):
    rig = AggregateRig(tmp_path)
    sibling = capture(branch="new-arrival", state=State.PARKED)
    blocked = []

    def simultaneous_admission():
        if cross_process:
            assert_admission_busy_in_child(tmp_path)
        with pytest.raises(RecoveryAdmissionDeferred, match="busy"):
            rig.aggregate.admit(sibling)
        assert rig.base.store.evidence_for_id(sibling.evidence.evidence_id) is None
        blocked.append(True)

    rig.remote.before_remove = simultaneous_admission
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    assert blocked
    rig.remote.before_remove = lambda: None
    assert "recovery-pending" not in rig.remote.labels
    outcome = rig.aggregate.admit(sibling)
    assert outcome.disposition.record_id == sibling.evidence.record_id
    assert "recovery-pending" in rig.remote.labels
    rig.intake.evidence_receive_sequence.assert_called_once_with(sibling.evidence)


def test_admission_keeps_durable_evidence_when_block_write_fails_then_replays(tmp_path):
    rig = AggregateRig(tmp_path)
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    sibling = capture(branch="new-arrival", state=State.PARKED)
    rig.remote.fail_add = "recovery-pending"
    with pytest.raises(RecoveryAdmissionDeferred, match="durable admission"):
        rig.aggregate.admit(sibling)
    assert rig.base.store.evidence_for_id(sibling.evidence.evidence_id) is not None
    rig.remote.fail_add = ""
    assert (
        rig.aggregate.admit(sibling).disposition.record_id == sibling.evidence.record_id
    )
    assert "recovery-pending" in rig.remote.labels
    assert len(rig.aggregate.for_issue(6914).dispositions) == 2


def test_gated_admission_preserves_global_receipt_order(tmp_path):
    rig = AggregateRig(tmp_path)
    later = capture(branch="new-arrival", run="later", state=State.PARKED)
    earlier = capture(branch="new-arrival", run="earlier", state=State.PARKED)
    rig.intake.evidence_receive_sequence.side_effect = lambda evidence: {
        "later": 2,
        "earlier": 1,
    }[evidence.identity.run_identity.run_id]
    rig.aggregate.admit(later)
    rig.aggregate.admit(earlier)
    assert (
        rig.base.store.get(later.evidence.record_id).evidence_id
        == later.evidence.evidence_id
    )
    assert rig.aggregate.evidence_for_id(earlier.evidence.evidence_id) is not None
    assert rig.aggregate.has_unresolved_work(6914)


@pytest.mark.parametrize("phase", [Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED])
def test_resume_preserves_later_operator_routing_edits_and_rebuilds_review_state(
    tmp_path, phase
):
    from issue_orchestrator.domain.models import OrchestratorState

    rig = AggregateRig(tmp_path)
    if phase is Phase.REVIEW_ROUTED:
        rig.remote.fail_remove = "recovery-pending"
        assert rig.base.invoke().status is FinalizationStatus.TRANSIENT
        rig.remote.fail_remove = ""
    else:
        assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    attempts = rig.base.store.publish_attempts(rig.base.claim.record_id)
    rig.remote.labels.discard("pr-pending")
    rig.remote.labels.add("operator-later")
    rig.reopen()
    state = OrchestratorState()
    result = rig.base.invoke(state)
    assert result.status is FinalizationStatus.FINALIZED
    assert "pr-pending" not in rig.remote.labels
    assert "operator-later" in rig.remote.labels
    assert len(state.discovered_reviews) == 1
    assert rig.base.store.publish_attempts(rig.base.claim.record_id) == attempts
    assert (
        rig.base.store.finalization_phase(rig.base.claim.record_id)
        is Phase.RECOVERY_CLEARED
    )


def test_crash_before_ack_preserves_readded_operator_failure_label(tmp_path):
    rig = AggregateRig(tmp_path)
    rig.remote.crash_after = "blocked-failed"
    with pytest.raises(Crash):
        rig.base.invoke()
    rig.remote.labels.add("blocked-failed")
    rig.remote.crash_after = ""
    rig.reopen()
    before = list(rig.remote.actions)
    result = rig.base.invoke()
    assert result.status is FinalizationStatus.TRANSIENT
    assert "ambiguous prior removal" in result.message
    assert "blocked-failed" in rig.remote.labels
    assert "recovery-pending" in rig.remote.labels
    assert rig.remote.actions == before
    rig.remote.labels.remove("blocked-failed")
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED


@pytest.mark.parametrize("cross_process", [False, True])
def test_recovery_release_serializes_other_lifecycle_acquisition(tmp_path, cross_process):
    rig = AggregateRig(tmp_path)
    own = HumanBlockRequest(
        6914, NeedsHumanCause.VALIDATED_WORK_DISPOSITION, "old failure",
        ValidatedWorkBlockSource(rig.base.claim.record_id),
    )
    other = HumanBlockRequest(6914, NeedsHumanCause.SESSION_LIFECYCLE, "new failure")
    assert rig.human.acquire(own) is BlockOutcome.HELD
    busy = []

    def acquire_during_removal():
        rig.remote.before_remove = lambda: None
        if cross_process:
            assert_human_acquisition_busy_in_child(tmp_path)
        busy.append(rig.human.acquire(other))
        assert rig.causes.needs_human_causes(6914) == frozenset({own.cause_key})

    rig.remote.before_remove = acquire_during_removal
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    assert busy == [BlockOutcome.FAILED]
    assert rig.human.acquire(other) is BlockOutcome.HELD
    rig.reopen()
    assert rig.aggregate.reconcile_issue_block(6914).status is RecoveryBlockReconcileStatus.RECONCILED
    assert rig.causes.needs_human_causes(6914) == frozenset({other.cause_key})
    assert "needs-human" in rig.remote.labels


def test_shared_block_queries_cannot_prune_an_in_progress_acquisition(tmp_path):
    rig = AggregateRig(tmp_path)
    request = HumanBlockRequest(6914, NeedsHumanCause.SESSION_LIFECYCLE, "new failure")
    seen = []

    def query_mid_acquire():
        rig.remote.after_read = lambda: None
        seen.append(rig.human.held_by_another_cause(
            6914, excluding=NeedsHumanCause.AGENT_COMPLETION
        ))
        seen.append(rig.human.force_clear(6914, "operator retry"))
        seen.append(rig.human.release(request))

    rig.remote.after_read = query_mid_acquire
    assert rig.human.acquire(request) is BlockOutcome.HELD
    assert seen == [True, BlockOutcome.FAILED, BlockOutcome.FAILED]
    assert rig.causes.needs_human_causes(6914) == frozenset({request.cause_key})
    assert "needs-human" in rig.remote.labels


@pytest.mark.parametrize("readd", [False, True])
def test_shared_block_crash_replay_preserves_operator_generation(tmp_path, readd):
    rig = AggregateRig(tmp_path)
    request = HumanBlockRequest(
        6914, NeedsHumanCause.VALIDATED_WORK_DISPOSITION, "old failure",
        ValidatedWorkBlockSource(rig.base.claim.record_id),
    )
    assert rig.human.acquire(request) is BlockOutcome.HELD
    rig.remote.crash_after = "needs-human"
    with pytest.raises(Crash):
        rig.base.invoke()
    assert request.cause_key in rig.causes.needs_human_causes(6914)
    if readd:
        rig.remote.labels.add("needs-human")
    rig.remote.crash_after = ""
    rig.reopen()
    before = list(rig.remote.actions)
    result = rig.base.invoke()
    if readd:
        assert result.status is FinalizationStatus.TRANSIENT
        assert rig.remote.actions == before
        assert "needs-human" in rig.remote.labels
        assert "recovery-pending" in rig.remote.labels
        assert request.cause_key in rig.causes.needs_human_causes(6914)
        rig.remote.labels.remove("needs-human")
    assert rig.base.invoke().status is FinalizationStatus.FINALIZED
    assert not rig.causes.needs_human_causes(6914)


def test_shared_block_new_generation_ends_prior_ambiguous_removal(tmp_path):
    rig = AggregateRig(tmp_path)
    first = HumanBlockRequest(6914, NeedsHumanCause.SESSION_LIFECYCLE, "first")
    second = HumanBlockRequest(6914, NeedsHumanCause.AGENT_COMPLETION, "second")
    assert rig.human.acquire(first) is BlockOutcome.HELD
    rig.remote.crash_after = "needs-human"
    with pytest.raises(Crash):
        rig.human.release(first)
    rig.remote.crash_after = ""
    rig.reopen()
    assert rig.human.acquire(second) is BlockOutcome.HELD
    assert rig.causes.needs_human_causes(6914) == frozenset({second.cause_key})
    assert rig.human.release(second) is BlockOutcome.CLEARED
    assert "needs-human" not in rig.remote.labels


def test_shared_block_later_cause_cannot_reset_ambiguous_present_generation(tmp_path):
    rig = AggregateRig(tmp_path)
    first = HumanBlockRequest(6914, NeedsHumanCause.SESSION_LIFECYCLE, "first")
    second = HumanBlockRequest(6914, NeedsHumanCause.AGENT_COMPLETION, "second")
    assert rig.human.acquire(first) is BlockOutcome.HELD
    rig.remote.crash_after = "needs-human"
    with pytest.raises(Crash):
        rig.human.release(first)
    rig.remote.crash_after = ""
    rig.remote.labels.add("needs-human")
    rig.reopen()
    assert rig.human.acquire(second) is BlockOutcome.HELD
    assert rig.human.release(first) is BlockOutcome.HELD_BY_ANOTHER_CAUSE
    assert rig.human.release(second) is BlockOutcome.FAILED
    assert rig.human.force_clear(6914, "retry") is BlockOutcome.FAILED
    assert "needs-human" in rig.remote.labels
    assert rig.causes.needs_human_causes(6914) == frozenset({second.cause_key})
