"""Private process-bound ownership, append-only CAS and phase progress."""

from dataclasses import replace
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from issue_orchestrator.domain.validated_work import (
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
)
from issue_orchestrator.domain.validated_work_claim import ClaimSecret
from issue_orchestrator.domain.validated_work_store import (
    DispositionPhase,
    FinalizationPhase as Phase,
    LineageResolutionRefusal as Refusal,
    PublishValidatedHeadStatus as Status,
)
from tests.unit.validated_work_support import (
    AT,
    LATER,
    OWNER,
    OTHER,
    ROOT,
    V,
    L,
    Rig,
    Liveness,
    begin,
    capture,
    changed_observations,
    claim,
    finalize,
)


def test_live_owner_cannot_be_stolen_even_by_same_process_or_future_time(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    assert (
        store.acquire_claim(
            token.record_id,
            expected_states=frozenset({State.QUEUED}),
            evidence_id=a.evidence.evidence_id,
        )
        is None
    )
    rival_live = Liveness(OTHER)
    rival = rig.open(rival_live)
    assert (
        rival.acquire_claim(
            token.record_id,
            expected_states=frozenset({State.QUEUED}),
            evidence_id=a.evidence.evidence_id,
        )
        is None
    )
    assert rival_live.checked == [OWNER]
    store.admit(changed_observations(a, captured_at="9999-01-01T00:00:00Z"))
    assert (
        rival.acquire_claim(
            token.record_id,
            expected_states=frozenset({State.QUEUED}),
            evidence_id=a.evidence.evidence_id,
        )
        is None
    )
    assert store.holds_claim(token)
    assert not rival.holds_claim(token)  # genuine handle presented by another process
    assert store.owner_of(token.record_id) == OWNER


def test_reading_fence_owner_and_hash_does_not_construct_a_claim(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        fence, stored_hash = conn.execute(
            "SELECT owner_fence,owner_claim_hash FROM validated_work_records"
        ).fetchone()
    forged = replace(token, fence=fence, secret=ClaimSecret())
    assert stored_hash == token.secret.digest()
    assert not store.holds_claim(forged)
    assert not store.relinquish_claim(forged)
    assert begin(store, forged) is None
    assert store.holds_claim(token)


def test_caller_supplied_digest_cannot_authenticate_readable_claim_facts(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        fence, stored_hash = conn.execute(
            "SELECT owner_fence,owner_claim_hash FROM validated_work_records"
        ).fetchone()
    forged = SimpleNamespace(
        record_id=token.record_id,
        fence=fence,
        owner=store.owner_of(token.record_id),
        secret=SimpleNamespace(digest=lambda: stored_hash),
    )
    before = store.get(token.record_id)
    assert not store.holds_claim(forged)
    assert not store.relinquish_claim(forged)
    assert begin(store, forged) is None
    assert store.get(token.record_id) == before
    assert store.publish_attempts(token.record_id) == ()
    assert store.holds_claim(token)
    assert begin(store, token) is not None


def test_positive_death_proof_mints_new_secret_and_invalidates_every_old_write(
    tmp_path,
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    old = claim(store, a)
    attempt = begin(store, old)
    b = capture(L)
    store.admit(b)
    successor = rig.open(Liveness(OTHER, dead={OWNER}))
    new = claim(successor, a)
    assert new.fence > old.fence
    assert new.secret.digest() != old.secret.digest()
    assert not store.holds_claim(old)
    assert not store.record_attempt_outcome(
        old, attempt, outcome=Status.PUBLISHED, failure=None, finished_at=LATER
    )
    assert not store.record_pr_number(old, pr_number=500)
    assert not store.record_finalization_phase(
        old, phase=Phase.REVIEW_ROUTED, recorded_at=LATER
    )
    assert not store.fail(
        old, failure=Failure.PUSH_FAILED, reason="stale", failed_at=LATER
    )
    assert (
        store.resolve_attached_evidence(old, record_id=old.record_id, resolved_at=LATER)
        is None
    )
    assert (
        store.resolve_published(
            old,
            record_id=old.record_id,
            published_head_sha=V,
            pre_push_expected=ROOT,
            finalized_at=LATER,
        )
        is Refusal.STALE_CLAIM
    )
    assert not store.relinquish_claim(old)
    assert successor.get(a.evidence.record_id).state is State.PUBLISHING
    assert (
        successor.get(b.evidence.record_id).failure
        is Failure.AWAITING_LINEAGE_PREDECESSOR
    )
    next_attempt = begin(successor, new)
    assert next_attempt.attempt_no == 2
    assert successor.publish_attempts(new.record_id)[0].outcome is None
    assert begin(successor, new) is None  # same live owner cannot overlap itself


def test_remote_host_death_claim_cannot_authorize_takeover(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    remote = Liveness(replace(OTHER, host="other-host"), dead={OWNER})
    rival = rig.open(remote)
    assert (
        rival.acquire_claim(
            token.record_id,
            expected_states=frozenset({State.QUEUED}),
            evidence_id=a.evidence.evidence_id,
        )
        is None
    )
    assert remote.checked == []


def test_attempt_cas_before_external_call_and_write_once_outcome(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    attempt = begin(store, token)
    assert attempt.attempt_no == 1
    reopened = rig.open()
    assert reopened.get(token.record_id).state is State.PUBLISHING
    assert reopened.publish_attempts(token.record_id) == (attempt,)
    assert begin(store, token) is None
    assert not store.record_attempt_outcome(
        token,
        replace(attempt, expected_remote_head=""),
        outcome=Status.PUBLISHED,
        failure=None,
        finished_at=LATER,
    )
    assert store.record_attempt_outcome(
        token,
        attempt,
        outcome=Status.TRANSIENT_FAILURE,
        failure=Failure.REMOTE_UNREADABLE,
        finished_at=LATER,
    )
    assert not store.record_attempt_outcome(
        token, attempt, outcome=Status.PUBLISHED, failure=None, finished_at=LATER
    )
    assert store.holds_claim(token)
    newer = begin(reopened, token)
    assert newer.attempt_no == 2
    assert not store.record_attempt_outcome(
        token, newer, outcome=Status.SUPERSEDED, failure=None, finished_at=LATER
    )
    assert store.publish_attempts(token.record_id)[1].outcome is None


@pytest.mark.parametrize("finished_at", ["", "  ", None, 12])
@pytest.mark.parametrize(
    ("outcome", "failure"),
    [
        (Status.PUBLISHED, None),
        (Status.TRANSIENT_FAILURE, Failure.REMOTE_UNREADABLE),
        (Status.REJECTED, Failure.PUSH_FAILED),
    ],
)
def test_invalid_outcome_timestamp_leaves_attempt_and_record_readable(
    tmp_path, finished_at, outcome, failure
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    attempt = begin(store, token)
    before = store.get(token.record_id)
    with pytest.raises(ValueError):
        store.record_attempt_outcome(
            token, attempt, outcome=outcome, failure=failure, finished_at=finished_at
        )
    reopened = rig.open()
    assert reopened.get(token.record_id) == before
    assert reopened.publish_attempts(token.record_id) == (attempt,)
    assert reopened.holds_claim(token)
    assert reopened.record_attempt_outcome(
        token, attempt, outcome=outcome, failure=failure, finished_at=LATER
    )
    assert reopened.publish_attempts(token.record_id) == (
        replace(attempt, outcome=outcome, failure=failure, finished_at=LATER),
    )


def test_attempt_budget_survives_restarts_and_exhaustion_is_unresolved(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    for n in range(1, 6):
        store = rig.open()
        attempt = begin(store, token)
        assert attempt.attempt_no == n
        assert store.record_attempt_outcome(
            token,
            attempt,
            outcome=Status.TRANSIENT_FAILURE,
            failure=Failure.REMOTE_UNREADABLE,
            finished_at=LATER,
        )
    assert store.get(token.record_id).state is State.FAILED
    assert len(store.publish_attempts(token.record_id)) == 5
    assert store.has_unresolved_work(6914)
    assert begin(store, token) is None
    assert store.evidence_for_retention(released_before="9999") == ()


def test_finalization_is_fenced_forward_only_and_cannot_infer_recovered(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    attempt = begin(store, token)
    assert not store.record_finalization_phase(
        token, phase=Phase.REVIEW_ROUTED, recorded_at=LATER
    )
    assert store.record_attempt_outcome(
        token, attempt, outcome=Status.PUBLISHED, failure=None, finished_at=LATER
    )
    assert store.holds_claim(token)
    assert begin(store, token) is None
    assert (
        store.resolve_published(
            token,
            record_id=token.record_id,
            published_head_sha=V,
            pre_push_expected=ROOT,
            finalized_at=LATER,
        )
        is Refusal.FINALIZATION_INCOMPLETE
    )
    assert not store.record_finalization_phase(
        token, phase=Phase.RECOVERY_CLEARED, recorded_at=LATER
    )
    assert not store.record_finalization_phase(
        token, phase=Phase.COMPLETE, recorded_at=LATER
    )
    assert store.record_finalization_phase(
        token, phase=Phase.REVIEW_ROUTED, recorded_at=LATER
    )
    assert store.record_finalization_phase(
        token, phase=Phase.REVIEW_ROUTED, recorded_at=LATER
    )
    assert not store.record_finalization_phase(
        token, phase=Phase.NOT_STARTED, recorded_at=LATER
    )
    assert store.record_finalization_phase(
        token, phase=Phase.RECOVERY_CLEARED, recorded_at=LATER
    )
    assert (
        store.resolve_published(
            token,
            record_id=token.record_id,
            published_head_sha=L,
            pre_push_expected=ROOT,
            finalized_at=LATER,
        )
        is Refusal.CONTAINMENT_UNPROVEN
    )
    store.resolve_published(
        token,
        record_id=token.record_id,
        published_head_sha=V,
        pre_push_expected=ROOT,
        finalized_at=LATER,
    )
    assert store.finalization_phase(token.record_id) is Phase.COMPLETE
    assert store.holds_claim(token)  # terminal state does not prove quiescence
    assert store.relinquish_claim(token)
    assert not store.holds_claim(token)


def test_observed_merge_refuses_live_publication_and_retained_claim(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    before = store.for_issue(6914)
    assert (
        store.resolve_observed_merge(
            record_id=token.record_id, merged_head_sha=V, observed_at=LATER
        )
        is Refusal.PUBLICATION_IN_FLIGHT
    )
    assert store.for_issue(6914) == before
    begin(store, token)
    assert (
        store.resolve_observed_merge(
            record_id=token.record_id, merged_head_sha=V, observed_at=LATER
        )
        is Refusal.PUBLICATION_IN_FLIGHT
    )


@pytest.mark.parametrize(
    "change", [{"pr_number": 999}, {"expected_remote_head_sha": None}, {}]
)
def test_parked_publication_requires_exact_unchanged_authority(tmp_path, change):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture(state=State.PARKED, reason="approval needed")
    store.admit(a)
    authority = store.evidence_for_id(a.evidence.evidence_id).evidence.authority
    token = claim(store, a)
    assert begin(store, token) is None
    store.admit(changed_observations(a, **change))
    now = store.evidence_for_id(a.evidence.evidence_id).evidence.authority
    assert (
        store.begin_publish_attempt(
            token,
            expected_attempt_no=0,
            target_head_sha=V,
            expected_remote_head=now.expected_remote_head_sha or "",
            phase=DispositionPhase.PRE_SUBMISSION,
            started_at=AT,
            authority=authority,
        )
        is None
    )
    assert store.get(token.record_id).state is State.PARKED
    assert store.publish_attempts(token.record_id) == ()
    assert begin(store, token, approved=True) is not None


def test_caller_equality_cannot_supply_parked_publication_consent(tmp_path):
    class FabricatedConsent:
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

    store = Rig(tmp_path / "work.sqlite").open()
    a = capture(state=State.PARKED, reason="approval needed")
    store.admit(a)
    token = claim(store, a)
    before = store.get(token.record_id)
    assert (
        store.begin_publish_attempt(
            token,
            expected_attempt_no=0,
            target_head_sha=V,
            expected_remote_head=ROOT,
            phase=DispositionPhase.PRE_SUBMISSION,
            started_at=AT,
            authority=FabricatedConsent(),
        )
        is None
    )
    assert store.get(token.record_id) == before
    assert store.publish_attempts(token.record_id) == ()
    assert begin(store, token, approved=True) is not None


def test_arbitrary_expected_states_do_not_authorize_publication(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture(state=State.FAILED, failure=Failure.ARTIFACT_MISSING)
    store.admit(a)
    token = claim(store, a)
    assert begin(store, token, approved=True) is None
    with pytest.raises(TypeError):
        store.acquire_claim(
            token.record_id,
            expected_states=frozenset({State.FAILED}),
            evidence_id=a.evidence.evidence_id,
            liveness=Liveness(OTHER, {OWNER}),
        )


def test_dead_retained_claim_cleanup_changes_only_ownership(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture(state=State.FAILED, failure=Failure.ARTIFACT_MISSING)
    store.admit(a)
    old = claim(store, a)
    before = store.evidence_for_id(a.evidence.evidence_id)
    successor = rig.open(Liveness(OTHER, {OWNER}))
    retained = successor.retained_claims(frozenset({State.FAILED}))
    assert len(retained) == 1 and retained[0].owner == OWNER
    new = claim(successor, a)
    assert successor.relinquish_claim(new)
    assert successor.owner_of(old.record_id) is None
    assert successor.evidence_for_id(a.evidence.evidence_id) == before
    assert successor.publish_attempts(old.record_id) == ()


def test_stop_reservation_refuses_relinquish_and_only_death_clears_it(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture(state=State.PARKED)
    store.admit(a)
    old = claim(store, a)
    # Arrange the future slice-7 reservation through its persisted schema.
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET stop_reserved_fence=?,stop_reserved_engine=?,stop_reservation_id='stop-id',stop_reserved_at=?",
            (old.fence, OWNER.instance_id, AT),
        )
    assert not store.relinquish_claim(old)
    assert store.holds_claim(old)
    successor = rig.open(Liveness(OTHER, {OWNER}))
    new = claim(successor, a)
    assert successor.relinquish_claim(new)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        assert conn.execute(
            "SELECT stop_reserved_fence,stop_reservation_id FROM validated_work_records"
        ).fetchone() == (-1, "")


def test_fenced_pr_fact_persists_without_refreshing_publication_baseline(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture(pr=None)
    store.admit(a)
    token = claim(store, a)
    attempt = begin(store, token)
    assert store.record_pr_number(token, pr_number=313)
    store = rig.open()
    evidence = store.evidence_for_id(a.evidence.evidence_id).evidence
    assert evidence.authority.pr_number == 313
    assert evidence.authority.expected_remote_head_sha == ROOT
    assert evidence.observation_revision == 1
    assert store.publish_attempts(token.record_id) == (attempt,)
    result = finalize(store, token, attempt)
    assert result.record.pr_number == 313
    assert not store.record_pr_number(token, pr_number=314)
    assert store.get(token.record_id).pr_number == 313


def test_truthy_unproven_death_cannot_transfer_authority(tmp_path):
    class UnprovenLiveness(Liveness):
        def is_provably_dead(self, owner):
            return "dead"

    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    a = capture()
    store.admit(a)
    token = claim(store, a)
    rival = rig.open(UnprovenLiveness(OTHER))
    with pytest.raises(TypeError, match="death proof"):
        rival.acquire_claim(
            token.record_id,
            expected_states=frozenset({State.QUEUED}),
            evidence_id=a.evidence.evidence_id,
        )
    assert store.holds_claim(token)
    assert store.owner_of(token.record_id) == OWNER


@pytest.mark.parametrize("released", [False, True])
def test_outcome_shape_validation_follows_claim_authentication(tmp_path, released):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    token = claim(store, admission)
    attempt = begin(store, token)
    if released:
        assert store.relinquish_claim(token)
    before = store.get(token.record_id)
    if released:
        assert not store.record_attempt_outcome(
            token, attempt, outcome=Status.PUBLISHED,
            failure=Failure.PUSH_FAILED, finished_at=LATER,
        )
    else:
        with pytest.raises(ValueError):
            store.record_attempt_outcome(
                token, attempt, outcome=Status.PUBLISHED,
                failure=Failure.PUSH_FAILED, finished_at=LATER,
            )
    reopened = rig.open()
    assert reopened.get(token.record_id) == before
    assert reopened.publish_attempts(token.record_id) == (attempt,)
