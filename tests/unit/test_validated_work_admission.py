"""All roles/states converge, attachments preserve their own capture judgement."""

from dataclasses import replace
import sqlite3
from contextlib import closing

import pytest

from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
)
from issue_orchestrator.domain.validated_work_store import (
    AdmissionStatus as Status,
    EvidenceRole,
)
from issue_orchestrator.domain.recovery_entry import RecoveryRecordRequest
from issue_orchestrator.domain.validated_work_remote_authority import (
    RemoteAuthorityRefreshRequest,
)
from tests.unit.validated_work_support import (
    AT,
    LATER,
    L,
    V,
    Rig,
    begin,
    capture,
    changed_observations,
    claim,
    finalize,
)


def test_replay_refresh_is_revised_only_before_submission(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture()
    assert store.admit(a).status is Status.ADMITTED
    changed = changed_observations(
        a, pr_number=200, expected_remote_head_sha=None, worktree_head_sha=L
    )
    assert store.admit(changed).status is Status.CONVERGED
    row = store.evidence_for_id(a.evidence.evidence_id).evidence
    assert row.observation_revision == 1
    assert row.authority.pr_number == 200
    assert row.authority.expected_remote_head_sha is None
    token = claim(store, a)
    attempt = begin(store, token)
    assert attempt is not None
    store.admit(a)
    frozen = store.evidence_for_id(a.evidence.evidence_id).evidence
    assert frozen == row
    finalize(store, token, attempt)
    store.admit(a)
    assert store.evidence_for_id(a.evidence.evidence_id).evidence == row


def test_observed_replay_replaces_only_the_transient_remote_base_gate(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture(
        state=State.PARKED,
        failure=Failure.REMOTE_UNREADABLE,
        reason="remote read failed",
        remote_status=RemoteBaselineStatus.UNOBSERVED,
    )
    store.admit(original)
    observed = replace(
        changed_observations(
            original,
            remote_baseline_status=RemoteBaselineStatus.OBSERVED,
            expected_remote_head_sha=V,
            pr_number=91,
        ),
        initial_state=State.QUEUED,
        initial_failure=None,
        initial_reason="remote authority observed",
    )

    outcome = store.admit(observed)

    row = store.evidence_for_id(original.evidence.evidence_id).evidence
    request, = store.drain_requests(after_record_id="", limit=10)
    assert outcome.status is Status.CONVERGED
    assert outcome.disposition.state is State.QUEUED
    assert row.admission.initial_failure is Failure.REMOTE_UNREADABLE
    assert row.base_state is State.QUEUED
    assert row.base_failure is None
    assert row.authority.remote_baseline_status is RemoteBaselineStatus.OBSERVED
    assert isinstance(request, RecoveryRecordRequest)


def test_inconsistent_observed_replay_cannot_block_the_drain_queue(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture(
        issue=1,
        state=State.PARKED,
        failure=Failure.REMOTE_UNREADABLE,
        reason="remote read failed",
        remote_status=RemoteBaselineStatus.UNOBSERVED,
    )
    other = capture(issue=2)
    store.admit(original)
    store.admit(other)
    inconsistent = changed_observations(
        original,
        remote_baseline_status=RemoteBaselineStatus.OBSERVED,
        expected_remote_head_sha=V,
        pr_number=91,
    )

    with pytest.raises(ValueError, match="cannot retain the remote-unreadable"):
        store.admit(inconsistent)

    requests = store.drain_requests(after_record_id="", limit=10)
    assert {request.record_id for request in requests} == {
        original.evidence.record_id,
        other.evidence.record_id,
    }
    assert any(isinstance(item, RemoteAuthorityRefreshRequest) for item in requests)
    assert any(isinstance(item, RecoveryRecordRequest) for item in requests)


def test_approval_required_unobserved_work_enters_remote_refresh_queue(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    historical = capture(
        state=State.PARKED,
        failure=None,
        reason="historical intake requires explicit approval",
        remote_status=RemoteBaselineStatus.UNOBSERVED,
    )
    store.admit(historical)

    request, = store.drain_requests(after_record_id="", limit=10)

    assert isinstance(request, RemoteAuthorityRefreshRequest)
    assert request.state is State.PARKED
    assert request.failure is None


def test_unobserved_replay_cannot_erase_last_observed_remote_authority(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture()
    store.admit(original)
    unavailable = replace(
        changed_observations(
            original,
            remote_baseline_status=RemoteBaselineStatus.UNOBSERVED,
            expected_remote_head_sha=None,
            pr_number=None,
        ),
        initial_state=State.PARKED,
        initial_failure=Failure.REMOTE_UNREADABLE,
        initial_reason="later read failed",
    )

    outcome = store.admit(unavailable)

    row = store.evidence_for_id(original.evidence.evidence_id).evidence
    assert outcome.disposition.state is State.QUEUED
    assert row.base_state is State.QUEUED
    assert row.authority.remote_baseline_status is RemoteBaselineStatus.OBSERVED
    assert row.authority.expected_remote_head_sha == original.evidence.observations.expected_remote_head_sha
    assert row.authority.pr_number == original.evidence.observations.pr_number


def test_observed_replay_preserves_a_later_durable_failure(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture(
        state=State.PARKED,
        failure=Failure.REMOTE_UNREADABLE,
        reason="remote read failed",
        remote_status=RemoteBaselineStatus.UNOBSERVED,
    )
    store.admit(original)
    token = claim(store, original)
    assert store.fail(
        token,
        failure=Failure.PUSH_FAILED,
        reason="publication failed",
        failed_at=LATER,
    )
    assert store.relinquish_claim(token)
    observed = replace(
        changed_observations(
            original,
            remote_baseline_status=RemoteBaselineStatus.OBSERVED,
            expected_remote_head_sha=V,
            pr_number=91,
        ),
        initial_state=State.QUEUED,
        initial_failure=None,
        initial_reason="remote authority observed",
    )

    outcome = store.admit(observed)

    row = store.evidence_for_id(original.evidence.evidence_id).evidence
    assert outcome.status is Status.CONVERGED
    assert outcome.disposition.state is State.FAILED
    assert outcome.disposition.failure is Failure.PUSH_FAILED
    assert outcome.disposition.reason == "publication failed"
    assert row.base_state is State.QUEUED
    assert row.base_failure is None
    assert row.authority.remote_baseline_status is RemoteBaselineStatus.OBSERVED


@pytest.mark.parametrize(
    "state,failure",
    [
        (State.QUEUED, None),
        (State.PARKED, Failure.WORKTREE_AHEAD_OF_VALIDATION),
        (State.FAILED, Failure.ARTIFACT_MISSING),
    ],
)
def test_new_evidence_supersedes_same_row_in_every_unresolved_resting_state(
    tmp_path, state, failure
):
    store = Rig(tmp_path / "work.sqlite").open()
    old = capture(state=state, failure=failure)
    new = capture(run="new")
    store.admit(old)
    outcome = store.admit(new)
    assert outcome.status is Status.SUPERSEDES
    assert outcome.disposition.state is State.QUEUED
    assert len(store.for_issue(6914).dispositions) == 1
    assert (
        store.evidence_for_id(old.evidence.evidence_id).evidence.role
        is EvidenceRole.SUPERSEDED
    )
    assert store.admit(old).status is Status.RETAINED
    assert store.get(old.evidence.record_id).evidence_id == new.evidence.evidence_id
    assert store.admit(new).status is Status.CONVERGED
    assert store.evidence_for_retention(released_before="9999-01-01T00:00:00+00:00") == ()


@pytest.mark.parametrize(
    "state,failure,reason",
    [
        (State.QUEUED, None, "eligible"),
        (State.PARKED, Failure.WORKTREE_AHEAD_OF_VALIDATION, "ahead needs approval"),
        (State.PARKED, Failure.WORKSPACE_INTEGRITY, "detached"),
        (State.PARKED, None, "historical intake"),
        (State.FAILED, Failure.ARTIFACT_MISSING, "missing bytes"),
    ],
)
def test_attached_drain_uses_original_disposition_on_every_nonpublishing_drain(
    tmp_path, state, failure, reason
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    original = capture()
    store.admit(original)
    token = claim(store, original)
    assert begin(store, token) is not None
    a = capture(run="attached-a", state=state, failure=failure, reason=reason, at=LATER)
    b = capture(
        run="attached-b",
        state=State.PARKED,
        reason="second judgement",
        at="2026-09-06T14:00:00+00:00",
    )
    for item in (a, b):
        assert store.admit(item).status is Status.ATTACHED
        assert store.admit(item).status is Status.ATTACHED
    assert len(store.attached_evidence(original.evidence.record_id)) == 2
    store.resolve_attached_evidence(token, record_id=token.record_id, resolved_at=LATER)
    assert len(store.attached_evidence(token.record_id)) == 2
    assert store.fail(
        token, failure=Failure.PUSH_FAILED, reason="failed", failed_at=LATER
    )
    store = rig.open()  # original capture directories are deliberately absent
    promoted = store.resolve_attached_evidence(
        token, record_id=token.record_id, resolved_at=LATER
    )
    assert (promoted.state, promoted.failure, promoted.reason) == (
        state,
        failure,
        reason,
    )
    assert promoted.evidence_id == a.evidence.evidence_id
    assert len(store.attached_evidence(token.record_id)) == 1
    second = store.resolve_attached_evidence(
        token, record_id=token.record_id, resolved_at=LATER
    )
    assert second.evidence_id == b.evidence.evidence_id
    assert second.state is State.PARKED and second.reason == "second judgement"
    assert store.attached_evidence(token.record_id) == ()
    assert store.admit(a).status is Status.RETAINED


def test_recovered_drain_retires_all_attached_and_retains_new_evidence(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture()
    store.admit(original)
    token = claim(store, original)
    attempt = begin(store, token)
    attachments = [capture(run=f"attached-{n}", at=LATER) for n in range(3)]
    for item in attachments:
        store.admit(item)
    finalize(store, token, attempt)
    store.resolve_attached_evidence(token, record_id=token.record_id, resolved_at=LATER)
    assert store.attached_evidence(token.record_id) == ()
    for item in attachments:
        assert (
            store.evidence_for_id(item.evidence.evidence_id).evidence.role
            is EvidenceRole.SUPERSEDED
        )
        assert store.admit(item).status is Status.RETAINED
    new = capture(run="post-publication")
    assert store.admit(new).status is Status.ALREADY_RECOVERED
    assert store.admit(new).status is Status.RETAINED
    assert len(store.evidence_for_retention(released_before="9999-01-01T00:00:00+00:00")) == 5
    assert not store.has_unresolved_work(6914)


def test_reopening_abandoned_work_requires_new_evidence(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    old = capture(state=State.PARKED)
    store.admit(old)
    # Arrange a valid historical row; the operator abandonment owner/API is
    # deliberately slice 7, not an arbitrary transition exposed by this store.
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET state='abandoned',resolution_kind='operator_abandoned',resolved_by='operator',resolution_reason='accepted loss',resolved_at=?,terminal_at=?,abandon_authority_json='retained-audit'",
            (AT, AT),
        )
    assert store.admit(old).status is Status.CONVERGED
    assert store.get(old.evidence.record_id).state is State.ABANDONED
    newer = capture(run="new", state=State.PARKED, reason="new approval required")
    result = store.admit(newer)
    assert result.status is Status.REOPENED
    assert result.disposition.state is State.PARKED
    assert result.disposition.resolution is None
    assert store.evidence_for_retention(released_before="9999-01-01T00:00:00+00:00") == ()
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        assert conn.execute(
            "SELECT abandon_authority_json,terminal_at FROM validated_work_records"
        ).fetchone() == ("retained-audit", "")


def test_new_capture_cannot_autoqueue_detached_or_ahead_work(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    for item in (capture(bound=False), capture(observed=L)):
        with pytest.raises(ValueError, match="approval-required"):
            store.admit(item)
    unobserved = capture(expected=None, pr=None)
    observations = replace(
        unobserved.evidence.observations,
        remote_baseline_status=RemoteBaselineStatus.UNOBSERVED,
    )
    with pytest.raises(ValueError, match="approval-required"):
        store.admit(replace(
            unobserved,
            evidence=replace(unobserved.evidence, observations=observations),
        ))
    assert not store.for_issue(6914).found_work


def test_admission_rollback_does_not_leave_partial_evidence_or_demote_current(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    old = capture()
    store.admit(old)
    before = store.evidence_for_id(old.evidence.evidence_id)
    rig.graph.raise_on.add(old.evidence.identity.key.validated_head_sha)
    newer = capture(run="replacement")
    with pytest.raises(RuntimeError, match="interruption"):
        store.admit(newer)
    assert store.evidence_for_id(newer.evidence.evidence_id) is None
    assert store.evidence_for_id(old.evidence.evidence_id) == before


def test_lost_established_database_cannot_be_read_as_no_work(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    store.admit(capture())
    rig.path.unlink()
    with pytest.raises(FileNotFoundError):
        store.has_unresolved_work(6914)


def test_replay_cannot_rewrite_attached_original_approval_requirement(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture()
    store.admit(original)
    token = claim(store, original)
    begin(store, token)
    attached = capture(
        run="later",
        state=State.PARKED,
        failure=Failure.WORKTREE_AHEAD_OF_VALIDATION,
        reason="operator must choose",
    )
    store.admit(attached)
    unsafe_replay = replace(
        attached,
        initial_state=State.QUEUED,
        initial_failure=None,
        initial_reason="auto",
    )
    assert store.admit(unsafe_replay).status is Status.ATTACHED
    store.fail(token, failure=Failure.PUSH_FAILED, reason="failed", failed_at=LATER)
    row = store.resolve_attached_evidence(
        token, record_id=token.record_id, resolved_at=LATER
    )
    assert row.state is State.PARKED and row.reason == "operator must choose"


def test_new_evidence_after_postpush_failure_can_begin_without_erasing_attempt_history(
    tmp_path,
):
    from issue_orchestrator.domain.validated_work_store import (
        PublishValidatedHeadStatus,
    )

    store = Rig(tmp_path / "work.sqlite").open()
    original = capture()
    store.admit(original)
    token = claim(store, original)
    attempt = begin(store, token)
    store.record_attempt_outcome(
        token,
        attempt,
        outcome=PublishValidatedHeadStatus.PUBLISHED,
        failure=None,
        finished_at=LATER,
    )
    store.fail(
        token,
        failure=Failure.REVIEW_ROUTING_FAILED,
        reason="routing failed",
        failed_at=LATER,
    )
    newer = capture(run="new")
    assert store.admit(newer).status is Status.ATTACHED
    # The still-active owner explicitly drains the new evidence before reuse.
    assert store.resolve_attached_evidence(
        token, record_id=token.record_id, resolved_at=LATER
    ).evidence_id == newer.evidence.evidence_id
    retry = begin(store, token)
    assert retry is not None and retry.attempt_no == 2
    assert retry.evidence_id == newer.evidence.evidence_id
    assert (
        store.publish_attempts(token.record_id)[0].evidence_id
        == original.evidence.evidence_id
    )
