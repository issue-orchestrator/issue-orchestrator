"""Public staged owner invariants across durable restart and interrupted effects."""

from dataclasses import replace

import pytest

from issue_orchestrator.domain.models import OrchestratorState, SessionHistoryEntry
from issue_orchestrator.domain.published_work_finalization import (
    FinalizationStatus as Status,
)
from issue_orchestrator.domain.retry_review_routing import RetryReviewRouting
from issue_orchestrator.domain.validated_work import (
    FinalizationPhase as Phase,
    ReviewDisposition,
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
)
from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy
from issue_orchestrator.ports.fresh_issue_reader import FreshIssueReadError
from tests.unit.staged_finalization_support import Crash, FinalizationRig
from tests.unit.validated_work_support import OTHER, OWNER, Liveness, capture, claim


POINTS = [
    "before:write",
    "after:write",
    "before:observe",
    "after:observe",
    "before:review_routed",
    "after:review_routed",
    "before:release",
    "before:remove:recovery-pending",
    "after:remove:recovery-pending",
    "before:remove:blocked-failed",
    "after:remove:blocked-failed",
    "observe:release",
    "after:release",
    "before:recovery_cleared",
    "after:recovery_cleared",
]


def test_routes_before_release_and_never_resolves_publication(tmp_path):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    state = OrchestratorState()
    rig.remote.labels.add("later-human-block")
    result = rig.invoke(state)
    assert result.status is Status.FINALIZED
    assert result.phase_reached is Phase.RECOVERY_CLEARED
    assert result.failure is None
    assert result.labels_added == ("pr-pending",)
    assert set(result.labels_removed) == {"recovery-pending", "blocked-failed"}
    assert rig.remote.labels == {"pr-pending", "later-human-block"}
    (review,) = state.discovered_reviews
    assert (review.issue_number, review.pr_number, review.branch_name) == (
        6914,
        91,
        "feature",
    )
    assert review.agent_label == "agent:developer"
    (history,) = state.session_history
    assert history.status == "completed"
    assert str(history.worktree_path) == "/preserved/worktree"
    assert rig.store.get(rig.claim.record_id).state is State.PUBLISHING
    assert rig.store.has_unresolved_work(6914)
    assert (
        rig.store.lineage_publication(
            rig.store.record_for_id(rig.claim.record_id).lineage_key
        )
        is None
    )
    assert rig.points.calls.index("after:observe") < rig.points.calls.index(
        "before:review_routed"
    )
    assert rig.points.calls.index("after:review_routed") < rig.points.calls.index(
        "before:release"
    )


@pytest.mark.parametrize("point", POINTS)
def test_interruption_reopens_real_phase_and_converges_without_early_release(
    tmp_path, point
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.points.at = point
    with pytest.raises(Crash):
        rig.invoke()
    phase = rig.store.finalization_phase(rig.claim.record_id)
    if phase is Phase.NOT_STARTED:
        assert "recovery-pending" in rig.remote.labels
    if "recovery-pending" not in rig.remote.labels:
        assert phase in {Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED}
        assert "pr-pending" in rig.remote.labels
    writes = len(rig.remote.added)
    rig.reopen()
    assert rig.store.finalization_phase(rig.claim.record_id) is phase
    state = OrchestratorState()
    result = rig.invoke(state)
    assert result.status is Status.FINALIZED
    assert result.phase_reached is Phase.RECOVERY_CLEARED
    assert len(rig.remote.added) == writes + (phase is Phase.NOT_STARTED)
    rig.invoke(state)
    assert len(state.discovered_reviews) == len(state.session_history) == 1
    assert state.completed_today == [6914]
    assert rig.store.get(rig.claim.record_id).state is State.PUBLISHING


@pytest.mark.parametrize(
    "phase", [Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED, Phase.COMPLETE]
)
def test_advanced_resume_replays_memory_without_overwriting_human_label_edits(
    tmp_path, phase
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    if phase is Phase.REVIEW_ROUTED:
        rig.aggregate.refuse = True
        assert rig.invoke().status is Status.TRANSIENT
    else:
        rig.invoke()
        if phase is Phase.COMPLETE:
            from tests.unit.validated_work_support import LATER, ROOT, V

            rig.store.resolve_published(
                rig.claim,
                record_id=rig.claim.record_id,
                published_head_sha=V,
                pre_push_expected=ROOT,
                finalized_at=LATER,
            )
    rig.reopen()
    rig.remote.labels.discard("pr-pending")
    rig.remote.labels.add("human-later")
    writes = len(rig.remote.added)
    state = OrchestratorState()
    # A stale NOT_STARTED caller must never replay completed label writes.
    result = rig.invoke(state, resume_from=Phase.NOT_STARTED)
    assert result.status is Status.FINALIZED
    assert "pr-pending" not in rig.remote.labels
    assert "human-later" in rig.remote.labels
    assert len(rig.remote.added) == writes
    assert len(state.discovered_reviews) == len(state.session_history) == 1
    rig.invoke(state)
    assert len(state.discovered_reviews) == len(state.session_history) == 1


@pytest.mark.parametrize(
    "point",
    [
        "before:write",
        "after:write",
        "before:observe",
        "after:observe",
        "before:release",
        "after:release",
    ],
)
def test_fresh_read_errors_are_transient_never_durable_failure(tmp_path, point):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.points.at, rig.points.error = (
        point,
        FreshIssueReadError("fresh read unavailable"),
    )
    result = rig.invoke()
    assert result.status is Status.TRANSIENT
    assert result.failure is None
    assert result.phase_reached is rig.store.finalization_phase(rig.claim.record_id)
    assert rig.store.get(rig.claim.record_id).state is State.PUBLISHING
    rig.reopen()
    assert rig.invoke().status is Status.FINALIZED


@pytest.mark.parametrize("mode", ["write-refused", "write-exception", "not-observed"])
def test_routing_failure_is_typed_only_after_durable_failure_write(tmp_path, mode):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    if mode == "write-refused":
        rig.remote.write_success = False
    elif mode == "write-exception":
        rig.points.at, rig.points.error = (
            "before:write",
            RuntimeError("label API failed"),
        )
    else:
        rig.remote.observe_written = False
    result = rig.invoke()
    assert result.status is Status.FAILED
    assert result.failure is Failure.REVIEW_ROUTING_FAILED
    assert result.phase_reached is Phase.NOT_STARTED
    row = rig.rig.open().get(rig.claim.record_id)
    assert row.state is State.FAILED and row.failure is Failure.REVIEW_ROUTING_FAILED
    assert "recovery-pending" in rig.remote.labels
    assert not rig.aggregate.requests


@pytest.mark.parametrize("mode", ["refused", "exception"])
def test_failure_persistence_failure_does_not_claim_failed(tmp_path, mode):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.remote.write_success = False
    if mode == "refused":
        rig.phases.refuse_failure = True
    else:
        rig.points.at, rig.points.error = (
            "before:fail",
            RuntimeError("store unavailable"),
        )
    assert rig.invoke().status is Status.TRANSIENT
    assert rig.store.get(rig.claim.record_id).state is State.PUBLISHING
    assert rig.store.finalization_phase(rig.claim.record_id) is Phase.NOT_STARTED


@pytest.mark.parametrize("phase", [Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED])
@pytest.mark.parametrize("mode", ["refused", "exception"])
def test_phase_commit_must_succeed_before_next_stage(tmp_path, phase, mode):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    if mode == "refused":
        rig.phases.refuse = phase
    else:
        rig.points.at, rig.points.error = (
            f"before:{phase.value}",
            RuntimeError("disk unavailable"),
        )
    result = rig.invoke()
    assert result.status is Status.TRANSIENT
    assert result.phase_reached is rig.store.finalization_phase(rig.claim.record_id)
    if phase is Phase.REVIEW_ROUTED:
        assert not rig.aggregate.requests
        assert rig.remote.removed == []
    rig.reopen()
    assert rig.invoke().status is Status.FINALIZED


@pytest.mark.parametrize("refused", [True, False])
def test_aggregate_owner_retains_sibling_blocks_and_can_refuse_release(
    tmp_path, refused
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    sibling = capture(branch="another-branch")
    rig.store.admit(sibling)
    rig.aggregate.refuse = refused
    result = rig.invoke()
    assert result.status is (Status.TRANSIENT if refused else Status.FINALIZED)
    assert result.phase_reached is (
        Phase.REVIEW_ROUTED if refused else Phase.RECOVERY_CLEARED
    )
    assert rig.remote.labels == {"recovery-pending", "blocked-failed", "pr-pending"}
    assert rig.store.get(sibling.evidence.record_id).unresolved
    assert rig.remote.removed == []
    (request,) = rig.aggregate.requests
    assert request.claim.record_id == rig.claim.record_id
    assert request.observed_blocking_labels == ("blocked-failed",)


@pytest.mark.parametrize(
    "disposition,configured,skip,halted,queued",
    [
        (ReviewDisposition.ROUTE_TO_PR_REVIEW, True, False, False, True),
        (ReviewDisposition.RESUME_REVIEW, True, False, False, True),
        (ReviewDisposition.ROUTE_TO_PR_REVIEW, False, False, False, False),
        (ReviewDisposition.ROUTE_TO_PR_REVIEW, True, True, False, False),
        (ReviewDisposition.RESUME_REVIEW, True, False, True, False),
        (ReviewDisposition.EXCHANGE_APPROVED, True, False, False, False),
    ],
)
def test_preserves_actual_session_review_policy(
    tmp_path, disposition, configured, skip, halted, queued
):
    rig = FinalizationRig(
        tmp_path / "work.sqlite", disposition=disposition, configured=configured
    )
    state = OrchestratorState()
    result = rig.invoke(
        state,
        routing=RetryReviewRouting(
            "feature", skip, disposition is ReviewDisposition.EXCHANGE_APPROVED, halted
        ),
    )
    assert result.status is Status.FINALIZED
    assert result.review_disposition is disposition
    assert bool(state.discovered_reviews) is queued
    assert "pr-pending" in rig.remote.labels
    assert "code-reviewed" not in rig.remote.labels


def test_history_dedupes_by_issue_and_pr_number_preserving_unrelated_state(tmp_path):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    entries = [
        SessionHistoryEntry(
            6914,
            "older spelling",
            "agent:developer",
            "completed",
            5,
            pr_url="https://github.com/owner/repo/pull/91/?old=1",
        ),
        SessionHistoryEntry(
            6914,
            "another PR",
            "agent:developer",
            "completed",
            5,
            pr_url="https://github.com/owner/repo/pull/92",
        ),
        SessionHistoryEntry(
            11,
            "another issue",
            "agent:developer",
            "completed",
            5,
            pr_url="https://github.com/owner/repo/pull/91",
        ),
    ]
    state = OrchestratorState(
        session_history=list(entries), failed_this_cycle={6914, 11}
    )
    rig.invoke(state)
    rig.invoke(state)
    assert state.session_history == entries
    assert len(state.discovered_reviews) == 1
    assert state.failed_this_cycle == {6914, 11}


@pytest.mark.parametrize(
    "point",
    [
        "after:admit",
        "after:write",
        "after:observe",
        "after:review_routed",
        "after:release",
        "after:recovery_cleared",
    ],
)
def test_real_claim_loss_stops_next_effect_and_retains_last_durable_phase(
    tmp_path, point
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    successor = rig.rig.open(Liveness(OTHER, dead={OWNER}))
    acquired = []
    rig.points.callbacks[point] = lambda: acquired.append(
        claim(successor, rig.admission)
    )
    state = OrchestratorState()
    result = rig.invoke(state)
    assert result.status is Status.TRANSIENT
    assert result.phase_reached is rig.store.finalization_phase(rig.claim.record_id)
    assert rig.points.calls[-1] == point
    assert rig.store.get(rig.claim.record_id).state is State.PUBLISHING
    if point == "after:write":
        assert not state.discovered_reviews and not state.session_history
    rig.store, rig.claim = successor, acquired[0]
    rig.points.callbacks.clear()
    rig.compose()
    assert rig.invoke(OrchestratorState()).status is Status.FINALIZED


@pytest.mark.parametrize(
    "point",
    [
        "before:write",
        "after:write",
        "before:observe",
        "after:review_routed",
        "before:release",
        "after:recovery_cleared",
    ],
)
def test_same_execution_owner_keeps_maintenance_out_for_entire_effect_lifetime(
    tmp_path, point
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    observed = []

    def maintenance():
        busy = rig.execution.try_enter(rig.claim.record_id)
        assert isinstance(busy, RecordExecutionBusy)
        observed.append(busy)

    rig.points.callbacks[point] = maintenance
    assert rig.invoke().status is Status.FINALIZED
    assert len(observed) == 1
    lease = rig.execution.try_enter(rig.claim.record_id)
    assert not isinstance(lease, RecordExecutionBusy)
    with lease as token:
        assert rig.execution.claim(token) is rig.claim
        assert rig.execution.relinquish(token)


def test_inactive_or_uncached_token_cannot_dispatch(tmp_path):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    lease = rig.execution.try_enter(rig.claim.record_id)
    with lease as token:
        request = rig.request(token, OrchestratorState())
        assert rig.finalizer.finalize(request).status is Status.TRANSIENT
    assert rig.finalizer.finalize(request).status is Status.TRANSIENT
    assert rig.points.calls == []


@pytest.mark.parametrize("field", ["pr_number", "review_disposition", "published_head"])
def test_store_rejects_mismatched_publication_before_label_or_memory_effects(
    tmp_path, field
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    lease = rig.execution.try_enter(rig.claim.record_id)
    with lease as token:
        rig.execution.remember_claim(token, rig.claim)
        state = OrchestratorState()
        request = rig.request(token, state)
        if field == "pr_number":
            target = replace(
                request.target,
                pr_number=92,
                pr_url="https://github.com/owner/repo/pull/92",
            )
            request = replace(request, target=target)
        elif field == "review_disposition":
            request = replace(
                request,
                target=replace(
                    request.target, review_disposition=ReviewDisposition.RESUME_REVIEW
                ),
            )
        else:
            # A valid claim with an uncompleted durable publication must also refuse.
            rig.store.fail(
                rig.claim,
                failure=Failure.PUSH_FAILED,
                reason="failed",
                failed_at="2026-09-06T13:00:00+00:00",
            )
        result = rig.finalizer.finalize(request)
    assert result.status is Status.TRANSIENT
    assert rig.remote.added == rig.remote.removed == []
    assert not state.discovered_reviews and not state.session_history


@pytest.mark.parametrize("phase", [Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED])
def test_lost_commit_acknowledgement_reports_durable_phase_without_next_stage(
    tmp_path, phase
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.points.at, rig.points.error = (
        f"after:{phase.value}",
        RuntimeError("acknowledgement lost"),
    )
    result = rig.invoke()
    assert result.status is Status.TRANSIENT
    assert result.phase_reached is phase
    assert result.phase_reached is rig.rig.open().finalization_phase(
        rig.claim.record_id
    )
    if phase is Phase.REVIEW_ROUTED:
        assert not rig.aggregate.requests
    rig.reopen()
    assert rig.invoke().status is Status.FINALIZED


def test_unreadable_store_is_transient_before_any_effect(tmp_path):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.points.at, rig.points.error = (
        "before:admit",
        RuntimeError("database unavailable"),
    )
    assert rig.invoke().status is Status.TRANSIENT
    assert rig.remote.added == rig.remote.removed == []


@pytest.mark.parametrize("checkpoint", range(1, 14))
def test_each_effect_and_each_memory_mutation_rechecks_real_store_claim(
    tmp_path, checkpoint
):
    """Interrupt the authority port at every check along a complete operation.

    SQLite still decides claim validity; no fake boolean can authorize an effect.
    The new process has a genuine fenced handle and the old process cannot
    mutate any port or state after that check.
    """
    from issue_orchestrator.control.validated_work_effects import (
        FencedValidatedWorkEffects,
    )
    from issue_orchestrator.control.staged_published_work_finalizer import (
        StagedPublishedWorkFinalizer,
    )
    from issue_orchestrator.control.retry_review_routing import RetryReviewPolicy

    rig = FinalizationRig(tmp_path / "work.sqlite")
    successor = rig.rig.open(Liveness(OTHER, dead={OWNER}))
    state = OrchestratorState()
    snapshots = []

    class Fence:
        count = 0

        def holds_claim(self, current):
            self.count += 1
            if self.count == checkpoint:
                claim(successor, rig.admission)
                snapshots.append(
                    (
                        list(rig.remote.added),
                        list(rig.remote.removed),
                        list(state.discovered_reviews),
                        list(state.session_history),
                        list(state.completed_today),
                        rig.store.finalization_phase(current.record_id),
                    )
                )
            return rig.store.holds_claim(current)

    fence = Fence()
    effects = FencedValidatedWorkEffects(execution=rig.execution, fence=fence)
    rig.aggregate.effects = effects
    rig.finalizer = StagedPublishedWorkFinalizer(
        effects=effects,
        phases=rig.phases,
        recovery=rig.aggregate,
        fresh_issue_reader=rig.remote,
        action_applier=rig.remote,
        review_policy=RetryReviewPolicy(code_review_agent_configured=True),
        routing_label="pr-pending",
    )
    result = rig.invoke(state)
    assert snapshots, f"checkpoint {checkpoint} was never exercised"
    assert result.status is Status.TRANSIENT
    assert snapshots[0] == (
        rig.remote.added,
        rig.remote.removed,
        state.discovered_reviews,
        state.session_history,
        state.completed_today,
        rig.store.finalization_phase(rig.claim.record_id),
    )


def test_copied_claim_handle_is_not_the_private_cached_capability(tmp_path):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    lease = rig.execution.try_enter(rig.claim.record_id)
    with lease as token:
        rig.execution.remember_claim(token, rig.claim)
        request = rig.request(token, OrchestratorState())
        duplicate = replace(rig.claim)
        assert rig.store.holds_claim(duplicate)
        result = rig.finalizer.finalize(replace(request, claim=duplicate))
        assert result.status is Status.TRANSIENT
    assert rig.points.calls == []


@pytest.mark.parametrize(
    "invalid",
    [
        "failed-without-failure",
        "transient-with-failure",
        "finalized-too-early",
        "failed-after-routing",
        "raw-status",
    ],
)
def test_outcome_rejects_false_success_and_failure_payloads(invalid):
    from issue_orchestrator.domain.published_work_finalization import (
        FinalizationOutcome,
    )

    values = dict(
        status=Status.TRANSIENT,
        phase_reached=Phase.NOT_STARTED,
        review_disposition=ReviewDisposition.ROUTE_TO_PR_REVIEW,
        labels_added=(),
        labels_removed=(),
        failure=None,
        message="retry",
    )
    if invalid == "failed-without-failure":
        values["status"] = Status.FAILED
    elif invalid == "transient-with-failure":
        values["failure"] = Failure.REVIEW_ROUTING_FAILED
    elif invalid == "finalized-too-early":
        values["status"] = Status.FINALIZED
    elif invalid == "failed-after-routing":
        values.update(
            status=Status.FAILED,
            failure=Failure.REVIEW_ROUTING_FAILED,
            phase_reached=Phase.REVIEW_ROUTED,
        )
    else:
        values["status"] = "transient"
    with pytest.raises(ValueError):
        FinalizationOutcome(**values)


@pytest.mark.parametrize("checkpoint", range(1, 14))
def test_authority_read_outages_are_transient_at_every_effect_boundary(
    tmp_path, checkpoint
):
    from issue_orchestrator.control.validated_work_effects import (
        FencedValidatedWorkEffects,
    )
    from issue_orchestrator.control.staged_published_work_finalizer import (
        StagedPublishedWorkFinalizer,
    )
    from issue_orchestrator.control.retry_review_routing import RetryReviewPolicy

    rig = FinalizationRig(tmp_path / "work.sqlite")
    state = OrchestratorState()

    class Fence:
        count = 0

        def holds_claim(self, current):
            self.count += 1
            if self.count == checkpoint:
                raise OSError("temporary authority read outage")
            return rig.store.holds_claim(current)

    fence = Fence()
    effects = FencedValidatedWorkEffects(execution=rig.execution, fence=fence)
    rig.aggregate.effects = effects
    rig.finalizer = StagedPublishedWorkFinalizer(
        effects=effects,
        phases=rig.phases,
        recovery=rig.aggregate,
        fresh_issue_reader=rig.remote,
        action_applier=rig.remote,
        review_policy=RetryReviewPolicy(code_review_agent_configured=True),
        routing_label="pr-pending",
    )
    result = rig.invoke(state)
    assert fence.count == checkpoint
    assert result.status is Status.TRANSIENT
    assert result.failure is None
    assert rig.store.get(rig.claim.record_id).state is State.PUBLISHING
    assert result.phase_reached is rig.rig.open().finalization_phase(
        rig.claim.record_id
    )
    if checkpoint == 2:
        assert rig.remote.added == []
    rig.reopen()
    assert rig.invoke(state).status is Status.FINALIZED
    assert len(state.discovered_reviews) == len(state.session_history) == 1


def test_lost_failure_ack_and_restart_report_matching_durable_failure(tmp_path):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.remote.write_success = False
    rig.points.at, rig.points.error = (
        "after:fail",
        RuntimeError("failure acknowledgement lost"),
    )
    first = rig.invoke()
    assert first.status is Status.FAILED
    assert first.phase_reached is Phase.NOT_STARTED
    assert first.failure is Failure.REVIEW_ROUTING_FAILED
    assert rig.store.get(rig.claim.record_id).failure is first.failure
    rig.reopen()
    state = OrchestratorState()
    replay = rig.invoke(state)
    assert replay.status is Status.FAILED
    assert replay.failure is first.failure
    assert replay.message == first.message
    assert rig.remote.added == rig.remote.removed == []
    assert not state.discovered_reviews and not state.session_history


def test_failed_checkpoint_requires_matching_publication_and_routing_cause(tmp_path):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.store.fail(
        rig.claim,
        failure=Failure.PUSH_FAILED,
        reason="other failure",
        failed_at="2026-09-06T13:00:00+00:00",
    )
    assert rig.invoke().status is Status.TRANSIENT
    assert rig.remote.added == []


def test_state_owner_review_intake_preserves_distinct_pairs_and_existing_completion_caller():
    from issue_orchestrator.domain.models import DiscoveredReview

    state = OrchestratorState()
    first = DiscoveredReview(1, 91, "https://github.com/owner/repo/pull/91", "feature")
    for review in (
        first,
        first,
        replace(first, issue_number=2),
        replace(first, pr_number=92),
    ):
        state.record_discovered_review(review)
    assert [
        (review.issue_number, review.pr_number) for review in state.discovered_reviews
    ] == [(1, 91), (2, 91), (1, 92)]
    state.record_completed_issue(1)
    state.record_completed_issue(1)
    state.record_completed_issue(2)
    assert state.completed_today == [1, 2]
    with pytest.raises(ValueError):
        state.record_publication_history(
            SessionHistoryEntry(1, "no publication", "agent:developer", "failed", 5)
        )


def test_unavailable_failure_readback_stays_transient_until_durable_result_can_be_read(
    tmp_path,
):
    rig = FinalizationRig(tmp_path / "work.sqlite")
    rig.remote.write_success = False

    def lose_ack_and_readback():
        rig.points.at = "before:admit"
        rig.points.error = OSError("checkpoint read unavailable")
        raise RuntimeError("failure acknowledgement lost")

    rig.points.callbacks["after:fail"] = lose_ack_and_readback
    result = rig.invoke()
    assert result.status is Status.TRANSIENT and result.failure is None
    assert rig.store.get(rig.claim.record_id).state is State.FAILED
    rig.reopen()
    result = rig.invoke()
    assert result.status is Status.FAILED
    assert result.failure is Failure.REVIEW_ROUTING_FAILED
