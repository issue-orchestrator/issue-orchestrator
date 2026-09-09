"""Claimed retry of transiently unavailable remote publication authority."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime
import json
import sqlite3

import pytest

from issue_orchestrator.control.remote_authority_refresh import (
    RemoteAuthorityRefreshOperation,
)
from issue_orchestrator.control.validated_work_effects import FencedValidatedWorkEffects
from issue_orchestrator.domain.publication_remote import (
    PublicationPullRequest,
    PublicationPrState,
    PublicationRemoteError,
)
from issue_orchestrator.domain.validated_work import (
    FinalizationPhase,
    PublishValidatedHeadStatus,
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkState,
    canonical_json,
)
from issue_orchestrator.domain.validated_work_capture import ValidatedWorkRemoteFacts
from issue_orchestrator.domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
from issue_orchestrator.domain.validated_work_gate import DispositionGate
from issue_orchestrator.domain.validated_work_remote_authority import (
    RemoteAuthorityRefreshRequest,
    refreshed_remote_authority,
)
from issue_orchestrator.execution.validated_work_execution import (
    LocalValidatedWorkExecutionOwner,
)
from tests.unit.validated_work_support import (
    AT,
    V,
    Rig,
    begin,
    capture,
    changed_observations,
    claim,
)


class Observer:
    def __init__(self, facts=None, error: Exception | None = None):
        self.facts = facts
        self.error = error
        self.requests = []

    def observe(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.facts


def matching_pr(number: int = 91) -> PublicationPullRequest:
    return PublicationPullRequest(
        number,
        f"https://github.com/owner/repo/pull/{number}",
        "owner/repo",
        "owner/repo",
        "feature",
        "main",
        V,
        PublicationPrState.OPEN,
        "candidate",
    )


def operation(store, observer) -> RemoteAuthorityRefreshOperation:
    execution = LocalValidatedWorkExecutionOwner(store)
    return RemoteAuthorityRefreshOperation(
        execution=execution,
        effects=FencedValidatedWorkEffects(execution=execution, fence=store),
        store=store,
        observer=observer,
        clock=lambda: datetime.fromisoformat(AT),
    )


def parked_unobserved():
    return capture(
        state=ValidatedWorkState.PARKED,
        failure=ValidatedWorkFailure.REMOTE_UNREADABLE,
        reason="capture read failed",
        remote_status=RemoteBaselineStatus.UNOBSERVED,
    )


def refresh_request(store) -> RemoteAuthorityRefreshRequest:
    request, = store.drain_requests(after_record_id="", limit=10)
    assert isinstance(request, RemoteAuthorityRefreshRequest)
    return request


@pytest.mark.parametrize(
    ("state", "failure", "remote_status"),
    [
        (ValidatedWorkState.QUEUED, None, RemoteBaselineStatus.UNOBSERVED),
        (ValidatedWorkState.PARKED, None, RemoteBaselineStatus.UNOBSERVED),
        (
            ValidatedWorkState.PARKED,
            ValidatedWorkFailure.REMOTE_UNREADABLE,
            RemoteBaselineStatus.OBSERVED,
        ),
        (
            ValidatedWorkState.PUBLISHING,
            ValidatedWorkFailure.REMOTE_UNREADABLE,
            RemoteBaselineStatus.UNOBSERVED,
        ),
    ],
)
def test_refresh_request_rejects_non_retryable_shapes(
    state, failure, remote_status,
):
    admission = parked_unobserved()
    evidence = admission.evidence
    key = evidence.identity.key
    authority = ValidatedWorkAuthoritySnapshot(
        evidence.record_id,
        evidence.evidence_id,
        0,
        key.validated_head_sha,
        key.branch_name,
        key.repo_slug,
        key.issue_number,
        None,
        None,
        remote_status,
    )

    with pytest.raises(ValueError):
        RemoteAuthorityRefreshRequest(authority, state, failure)


def test_exact_healthy_refresh_queues_and_preserves_capture_audit(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = parked_unobserved()
    store.admit(admission)
    request = refresh_request(store)

    outcome = operation(
        store, Observer(ValidatedWorkRemoteFacts(V, (matching_pr(),)))
    ).run(request)

    disposition = store.get(admission.evidence.record_id)
    row, = store.retained_evidence(admission.evidence.identity.key.issue_number)
    assert outcome.failure is None
    assert disposition.state is ValidatedWorkState.QUEUED
    assert disposition.failure is None
    assert row.admission.initial_state is ValidatedWorkState.PARKED
    assert row.admission.initial_failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert row.base_state is ValidatedWorkState.QUEUED
    assert row.base_failure is None
    assert row.observation_revision == 1
    assert row.admission.evidence.observations.remote_baseline_status is RemoteBaselineStatus.OBSERVED
    assert row.admission.evidence.observations.expected_remote_head_sha == V
    assert row.admission.evidence.observations.pr_number == 91
    derived = DispositionGate(
        ValidatedWorkState.PARKED,
        ValidatedWorkFailure.ANCESTOR_OF_PENDING_HEAD,
        "lineage restriction",
    )
    assert derived.restore(DispositionGate.from_evidence(row)).state is ValidatedWorkState.QUEUED


def test_failed_remote_read_keeps_same_retryable_snapshot(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    store.admit(parked_unobserved())
    request = refresh_request(store)

    outcome = operation(
        store, Observer(error=PublicationRemoteError("quota unavailable"))
    ).run(request)

    assert outcome.failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert refresh_request(store) == request


def test_stale_selected_revision_is_refused_before_remote_read(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = parked_unobserved()
    store.admit(admission)
    request = refresh_request(store)
    changed = changed_observations(
        admission,
        observed_blocking_labels=("new-generation",),
    )
    store.admit(changed)
    observer = Observer(ValidatedWorkRemoteFacts(V, (matching_pr(),)))

    outcome = operation(store, observer).run(request)

    assert "no longer current" in outcome.message
    assert observer.requests == []
    assert store.get(admission.evidence.record_id).state is ValidatedWorkState.PARKED


def test_store_rejects_refresh_that_rewrites_capture_facts(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = parked_unobserved()
    store.admit(admission)
    request = refresh_request(store)
    owned = claim(store, admission)
    record = store.record_for_id(request.record_id)
    decision = refreshed_remote_authority(
        record, ValidatedWorkRemoteFacts(V, (matching_pr(),))
    )
    forged = replace(
        decision,
        observations=replace(decision.observations, captured_at="2026-09-07T00:00:00Z"),
    )

    with pytest.raises(ValueError, match="immutable capture facts"):
        store.refresh_remote_authority(
            owned, request, forged, refreshed_at=AT
        )

    assert store.relinquish_claim(owned)
    assert refresh_request(store) == request


def test_conflicting_pr_parks_with_durable_refreshed_gate(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = parked_unobserved()
    store.admit(admission)

    outcome = operation(
        store,
        Observer(ValidatedWorkRemoteFacts(V, (matching_pr(91), matching_pr(92)))),
    ).run(refresh_request(store))

    disposition = store.get(admission.evidence.record_id)
    row, = store.retained_evidence(admission.evidence.identity.key.issue_number)
    assert outcome.failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR
    assert disposition.state is ValidatedWorkState.PARKED
    assert disposition.failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR
    assert row.base_state is ValidatedWorkState.PARKED
    assert row.base_failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR
    assert store.drain_requests(after_record_id="", limit=10) == ()


def test_publishing_unobserved_refresh_preserves_in_flight_lifecycle(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = parked_unobserved()
    store.admit(admission)
    owned = claim(store, admission)
    attempt = begin(store, owned, approved=True)
    assert attempt is not None
    assert store.record_attempt_outcome(
        owned,
        attempt,
        outcome=PublishValidatedHeadStatus.PUBLISHED,
        failure=None,
        finished_at=AT,
    )
    completed, = store.publish_attempts(admission.evidence.record_id)
    assert store.relinquish_claim(owned)
    assert store.get(admission.evidence.record_id).state is ValidatedWorkState.PUBLISHING

    operation(
        store, Observer(ValidatedWorkRemoteFacts(V, (matching_pr(),)))
    ).run(refresh_request(store))

    disposition = store.get(admission.evidence.record_id)
    row, = store.retained_evidence(admission.evidence.identity.key.issue_number)
    assert disposition.state is ValidatedWorkState.PUBLISHING
    assert row.base_state is ValidatedWorkState.QUEUED
    assert row.admission.evidence.observations.remote_baseline_status is RemoteBaselineStatus.OBSERVED
    assert completed.succeeded_for(row.authority)

    successor = claim(store, admission)
    assert store.record_finalization_phase(
        successor,
        phase=FinalizationPhase.REVIEW_ROUTED,
        recorded_at=AT,
    )
    assert store.record_finalization_phase(
        successor,
        phase=FinalizationPhase.RECOVERY_CLEARED,
        recorded_at=AT,
    )
    resolution = store.resolve_published(
        successor,
        record_id=successor.record_id,
        published_head_sha=V,
        pre_push_expected=V,
        finalized_at=AT,
    )
    assert resolution.record.state is ValidatedWorkState.RECOVERED


def test_migrated_success_with_nonempty_baseline_finalizes_after_exact_refresh(
    tmp_path,
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    owned = claim(store, admission)
    attempt = begin(store, owned, approved=True)
    assert attempt is not None and attempt.expected_remote_head
    assert store.record_attempt_outcome(
        owned,
        attempt,
        outcome=PublishValidatedHeadStatus.PUBLISHED,
        failure=None,
        finished_at=AT,
    )
    assert store.relinquish_claim(owned)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        observations = json.loads(canonical_json(admission.evidence.observations))
        observations.pop("remote_baseline_status")
        conn.execute(
            "UPDATE validated_work_evidence SET observations=? WHERE evidence_id=?",
            (canonical_json(observations), admission.evidence.evidence_id),
        )

    store = rig.open()
    row, = store.retained_evidence(admission.evidence.identity.key.issue_number)
    assert row.authority.remote_baseline_status is RemoteBaselineStatus.UNOBSERVED
    operation(
        store, Observer(ValidatedWorkRemoteFacts(V, (matching_pr(),)))
    ).run(refresh_request(store))

    successor = claim(store, admission)
    for phase in (FinalizationPhase.REVIEW_ROUTED, FinalizationPhase.RECOVERY_CLEARED):
        assert store.record_finalization_phase(successor, phase=phase, recorded_at=AT)
    resolution = store.resolve_published(
        successor,
        record_id=successor.record_id,
        published_head_sha=V,
        pre_push_expected=V,
        finalized_at=AT,
    )
    assert resolution.record.state is ValidatedWorkState.RECOVERED


def test_publishing_refresh_parks_an_observed_pr_conflict(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    admission = parked_unobserved()
    store.admit(admission)
    owned = claim(store, admission)
    assert begin(store, owned, approved=True) is not None
    assert store.relinquish_claim(owned)

    outcome = operation(
        store,
        Observer(ValidatedWorkRemoteFacts(V, (matching_pr(91), matching_pr(92)))),
    ).run(refresh_request(store))

    disposition = store.get(admission.evidence.record_id)
    assert outcome.failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR
    assert disposition.state is ValidatedWorkState.PARKED
    assert disposition.failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR
    assert store.drain_requests(after_record_id="", limit=10) == ()
