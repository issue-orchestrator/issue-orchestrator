"""Arrival-order-independent lineage, verified containment and baseline provenance."""

import pytest

from issue_orchestrator.domain.validated_work import (
    LineageRole,
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
    canonical_lineage_key,
)
from issue_orchestrator.domain.validated_work_store import (
    LineageResolutionRefusal as Refusal,
    PublicationProvenance,
    PublicationResolution,
)
from tests.unit.validated_work_support import (
    AT,
    LATER,
    ROOT,
    V,
    L,
    TIP,
    DIVERGENT,
    Rig,
    begin,
    capture,
    claim,
    finalize,
)


@pytest.mark.parametrize(
    "heads", [(V, L), (L, V), (TIP, V, L), (V, L, TIP), (L, TIP, V)]
)
def test_ancestor_and_descendant_admit_in_any_order_and_resolve_together(
    tmp_path, heads
):
    store = Rig(tmp_path / "work.sqlite").open()
    for head in heads:
        store.admit(capture(head))
    tip = TIP if TIP in heads else L
    batch = store.for_issue(6914)
    assert len([d for d in batch.dispositions if d.state is State.QUEUED]) == 1
    for d in batch.dispositions:
        if d.key.validated_head_sha != tip:
            assert d.lineage_role is LineageRole.ANCESTOR and d.state is State.PARKED
    token = claim(store, capture(tip))
    result = finalize(store, token, begin(store, token))
    assert len(result.resolved_ancestors) == len(heads) - 1
    assert not store.has_unresolved_work(6914)
    assert all(d.published_head_sha == tip for d in store.for_issue(6914).dispositions)


@pytest.mark.parametrize(
    "state,failure,reason",
    [
        (State.QUEUED, None, "automatic"),
        (State.PARKED, Failure.WORKTREE_AHEAD_OF_VALIDATION, "ahead"),
        (State.PARKED, Failure.WORKSPACE_INTEGRITY, "detached"),
        (State.PARKED, None, "historical"),
        (State.FAILED, Failure.ARTIFACT_MISSING, "failed"),
    ],
)
@pytest.mark.parametrize("arrives_during_publish", [False, True])
def test_lineage_promotion_never_overrides_evidence_gate(
    tmp_path, state, failure, reason, arrives_during_publish
):
    store = Rig(tmp_path / "work.sqlite").open()
    predecessor = capture(V)
    successor = capture(L, state=state, failure=failure, reason=reason)
    store.admit(predecessor)
    token = claim(store, predecessor)
    attempt = begin(store, token)
    if arrives_during_publish:
        result = store.admit(successor).disposition
        assert (
            result.state is State.PARKED and result.lineage_role is LineageRole.PENDING
        )
    finalize(store, token, attempt)
    if not arrives_during_publish:
        store.admit(successor)
    row = store.get(successor.evidence.record_id)
    assert row.lineage_role is LineageRole.HEAD
    assert (row.state, row.failure, row.reason) == (state, failure, reason)
    evidence = store.evidence_for_id(row.evidence_id).evidence
    assert evidence.authority.expected_remote_head_sha == V
    assert evidence.observation_revision == 1


@pytest.mark.parametrize("kind", ["ancestor", "descendant", "divergent"])
def test_arrivals_behind_publication_have_record_scoped_pending_state(tmp_path, kind):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture(L)
    store.admit(original)
    token = claim(store, original)
    attempt = begin(store, token)
    waiting = capture({"ancestor": V, "descendant": TIP, "divergent": DIVERGENT}[kind])
    pending = store.admit(waiting).disposition
    assert pending.state is State.PARKED
    assert pending.lineage_role is LineageRole.PENDING
    assert pending.failure is Failure.AWAITING_LINEAGE_PREDECESSOR
    assert store.attached_evidence(token.record_id) == ()
    result = finalize(store, token, attempt)
    assert len(result.classified_waiters) == 1
    row = store.get(waiting.evidence.record_id)
    expected = {
        "ancestor": State.RECOVERED,
        "descendant": State.QUEUED,
        "divergent": State.PARKED,
    }
    assert row.state is expected[kind]
    assert row.lineage_role is not LineageRole.PENDING


@pytest.mark.parametrize("after", [False, True])
def test_bad_ancestor_escrow_stays_failed_in_both_arrival_orders(tmp_path, after):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    ancestor, descendant = capture(V), capture(L)
    rig.artifacts.invalid.add(ancestor.evidence.record_id)
    if not after:
        store.admit(ancestor)
    store.admit(descendant)
    token = claim(store, descendant)
    result = finalize(store, token, begin(store, token))
    if after:
        store.admit(ancestor)
    else:
        assert [r.record_id for r in result.failed_ancestors] == [
            ancestor.evidence.record_id
        ]
    row = store.get(ancestor.evidence.record_id)
    assert row.state is State.FAILED and row.failure is Failure.ARTIFACT_HASH_MISMATCH
    assert store.has_unresolved_work(6914)


@pytest.mark.parametrize("via", ["push", "merge"])
@pytest.mark.parametrize(
    "expected,automatic", [(ROOT, True), (L, True), (None, False), (DIVERGENT, False)]
)
def test_baseline_advance_is_proven_from_publication_fact(
    tmp_path, via, expected, automatic
):
    store = Rig(tmp_path / "work.sqlite").open()
    predecessor = capture(L)
    store.admit(predecessor)
    if via == "push":
        token = claim(store, predecessor)
        finalize(store, token, begin(store, token))
    else:
        assert isinstance(
            store.resolve_observed_merge(
                record_id=predecessor.evidence.record_id,
                merged_head_sha=L,
                observed_at=LATER,
            ),
            PublicationResolution,
        )
    later = capture(TIP, expected=expected)
    row = store.admit(later).disposition
    allowed = automatic if via == "push" else expected == L
    assert row.state is (State.QUEUED if allowed else State.PARKED)
    if not allowed:
        assert row.failure is Failure.REMOTE_BASELINE_UNPROVEN
    evidence = store.evidence_for_id(row.evidence_id).evidence
    assert evidence.authority.expected_remote_head_sha == (L if allowed else expected)


@pytest.mark.parametrize("via", ["push", "merge"])
def test_late_ancestor_resolves_from_both_publication_routes(tmp_path, via):
    store = Rig(tmp_path / "work.sqlite").open()
    shipped = capture(L)
    store.admit(shipped)
    if via == "push":
        token = claim(store, shipped)
        result = finalize(store, token, begin(store, token))
        assert result.lineage.published_via is PublicationProvenance.PUSHED_BY_OWNER
    else:
        result = store.resolve_observed_merge(
            record_id=shipped.evidence.record_id, merged_head_sha=TIP, observed_at=LATER
        )
        assert result.lineage.published_via is PublicationProvenance.OBSERVED_MERGE
    late = store.admit(capture(V)).disposition
    assert late.state is State.RECOVERED
    assert not store.has_unresolved_work(6914)


def test_divergent_and_different_branch_heads_are_distinct_work(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    store.admit(capture(V))
    store.admit(capture(DIVERGENT))
    assert all(
        d.state is State.PARKED and d.lineage_role is LineageRole.DIVERGENT
        for d in store.for_issue(6914).dispositions
    )
    other = store.admit(capture(V, branch="independent")).disposition
    assert other.state is State.QUEUED
    first = capture(V)
    result = store.resolve_observed_merge(
        record_id=first.evidence.record_id, merged_head_sha=L, observed_at=LATER
    )
    assert isinstance(result, PublicationResolution)
    assert store.get(capture(DIVERGENT).evidence.record_id).state is State.PARKED
    assert store.has_unresolved_work(6914)


def test_unreachable_row_fails_without_mutating_readable_peer(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    first = store.admit(capture(V)).disposition
    rig.graph.missing.add(L)
    failed = store.admit(capture(L)).disposition
    assert (
        failed.state is State.FAILED
        and failed.failure is Failure.VALIDATION_SHA_MISMATCH
    )
    assert store.get(first.record_id) == first


@pytest.mark.parametrize("via", ["push", "merge"])
def test_resolution_rolls_back_subject_fact_and_ancestors_when_waiter_verification_raises(
    tmp_path, via
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    ancestor, head = capture(V), capture(L)
    store.admit(ancestor)
    store.admit(head)
    if via == "push":
        token = claim(store, head)
        attempt = begin(store, token)
        from issue_orchestrator.domain.validated_work_store import (
            FinalizationPhase,
            PublishValidatedHeadStatus,
        )

        store.record_attempt_outcome(
            token,
            attempt,
            outcome=PublishValidatedHeadStatus.PUBLISHED,
            failure=None,
            finished_at=LATER,
        )
        for phase in (
            FinalizationPhase.REVIEW_ROUTED,
            FinalizationPhase.RECOVERY_CLEARED,
        ):
            store.record_finalization_phase(token, phase=phase, recorded_at=LATER)
    before = store.for_issue(6914)
    rig.artifacts.raise_on.add(ancestor.evidence.record_id)
    with pytest.raises(RuntimeError, match="interruption"):
        if via == "push":
            store.resolve_published(
                token,
                record_id=token.record_id,
                published_head_sha=L,
                pre_push_expected=ROOT,
                finalized_at=LATER,
            )
        else:
            store.resolve_observed_merge(
                record_id=head.evidence.record_id, merged_head_sha=L, observed_at=LATER
            )
    assert store.for_issue(6914) == before
    assert (
        store.lineage_publication(canonical_lineage_key(head.evidence.identity.key))
        is None
    )


def test_publication_fact_cannot_move_backward_or_accept_unproven_containment(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    a, b = capture(L), capture(V)
    store.admit(a)
    store.resolve_observed_merge(
        record_id=a.evidence.record_id, merged_head_sha=TIP, observed_at=LATER
    )
    store.admit(b)
    before = store.lineage_publication(canonical_lineage_key(a.evidence.identity.key))
    assert (
        store.resolve_observed_merge(
            record_id=b.evidence.record_id, merged_head_sha=V, observed_at=LATER
        )
        is Refusal.NOT_A_DESCENDANT
    )
    assert (
        store.resolve_observed_merge(
            record_id=b.evidence.record_id, merged_head_sha=DIVERGENT, observed_at=LATER
        )
        is Refusal.CONTAINMENT_UNPROVEN
    )
    assert store.lineage_publication(before.lineage_key) == before


@pytest.mark.parametrize(
    "failure",
    [
        Failure.ANCESTOR_OF_PENDING_HEAD,
        Failure.DIVERGENT_VALIDATED_HEADS,
        Failure.AWAITING_LINEAGE_PREDECESSOR,
        Failure.REMOTE_BASELINE_UNPROVEN,
        Failure.PUSH_FAILED,
        Failure.ARTIFACT_MISSING,
    ],
)
def test_failed_predecessor_classifies_waiter_without_moving_baseline(
    tmp_path, failure
):
    store = Rig(tmp_path / "work.sqlite").open()
    a, b = capture(V), capture(L)
    store.admit(a)
    token = claim(store, a)
    begin(store, token)
    store.admit(b)
    assert store.fail(token, failure=failure, reason="failed", failed_at=LATER)
    row = store.get(b.evidence.record_id)
    assert row.state is State.QUEUED and row.lineage_role is LineageRole.HEAD
    assert (
        store.evidence_for_id(
            row.evidence_id
        ).evidence.authority.expected_remote_head_sha
        == ROOT
    )
    assert store.get(a.evidence.record_id).state is State.FAILED
    assert store.get(a.evidence.record_id).failure is failure


@pytest.mark.parametrize(
    "failure",
    [
        Failure.ANCESTOR_OF_PENDING_HEAD,
        Failure.DIVERGENT_VALIDATED_HEADS,
        Failure.AWAITING_LINEAGE_PREDECESSOR,
        Failure.REMOTE_BASELINE_UNPROVEN,
        Failure.PUSH_FAILED,
    ],
)
def test_failed_waiter_is_not_released_by_predecessor_publication(tmp_path, failure):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    predecessor, waiter = capture(V), capture(L)
    store.admit(predecessor)
    owner = claim(store, predecessor)
    attempt = begin(store, owner)
    store.admit(waiter)
    token = claim(store, waiter)
    assert store.fail(token, failure=failure, reason="waiter failed", failed_at=LATER)
    finalize(store, owner, attempt)
    reopened = rig.open()
    row = reopened.get(token.record_id)
    assert (row.state, row.failure, row.reason) == (
        State.FAILED,
        failure,
        "waiter failed",
    )
    assert row.lineage_role is LineageRole.HEAD
    assert begin(reopened, token) is None


def test_attached_promotion_cannot_erase_existing_lineage_restriction(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    original = capture(V)
    store.admit(original)
    token = claim(store, original)
    begin(store, token)
    attached = capture(V, run="attached")
    store.admit(attached)
    store.admit(capture(DIVERGENT))
    store.fail(token, failure=Failure.PUSH_FAILED, reason="failed", failed_at=LATER)
    promoted = store.resolve_attached_evidence(
        token, record_id=token.record_id, resolved_at=LATER
    )
    assert (
        promoted.state is State.PARKED
        and promoted.lineage_role is LineageRole.DIVERGENT
    )


def test_explicit_current_authority_selects_one_divergent_head(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    selected, other = capture(V), capture(DIVERGENT)
    store.admit(selected)
    store.admit(other)
    token = claim(store, selected)
    assert begin(store, token) is None
    attempt = begin(store, token, approved=True)
    assert attempt is not None
    assert store.get(selected.evidence.record_id).lineage_role is LineageRole.HEAD
    finalize(store, token, attempt)
    assert store.get(other.evidence.record_id).state is State.PARKED
    assert (
        store.get(other.evidence.record_id).failure is Failure.DIVERGENT_VALIDATED_HEADS
    )


def test_explicit_ancestor_choice_defers_queued_descendant_atomically(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    selected, later = capture(V), capture(L)
    store.admit(selected)
    store.admit(later)
    token = claim(store, selected)
    attempt = begin(store, token, approved=True)
    assert attempt is not None
    pending = store.record_for_id(later.evidence.record_id)
    assert pending.waits_on_record_id == token.record_id
    assert pending.disposition.lineage_role is LineageRole.PENDING
    finalize(store, token, attempt)
    released = store.record_for_id(later.evidence.record_id)
    assert released.waits_on_record_id == ""
    assert released.disposition.state is State.QUEUED
    assert released.current_evidence.authority.expected_remote_head_sha == V


def test_late_unreachable_comparison_cannot_park_a_readable_peer(tmp_path):
    from issue_orchestrator.domain.validated_work_store import AncestryRelation
    from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
    from tests.unit.validated_work_support import GraphAncestry

    class PairUnreachable(GraphAncestry):
        def compare(self, left, right):
            a, b = left.key.validated_head_sha, right.key.validated_head_sha
            if a != b and a == L:
                return AncestryRelation.LEFT_UNREACHABLE
            if a != b and b == L:
                return AncestryRelation.RIGHT_UNREACHABLE
            return super().compare(left, right)

    rig = Rig(tmp_path / "work.sqlite")
    store = SqliteValidatedWorkStore(
        rig.path,
        ancestry=PairUnreachable(),
        artifacts=rig.artifacts,
        liveness=rig.liveness,
    )
    a = capture(V)
    store.admit(a)
    before = store.record_for_id(a.evidence.record_id)
    store.admit(capture(L, at=LATER))
    assert store.record_for_id(a.evidence.record_id) == before
    assert (
        store.get(capture(L).evidence.record_id).failure
        is Failure.VALIDATION_SHA_MISMATCH
    )


def test_repeated_verified_merge_preserves_resolution_audit_and_retention_start(
    tmp_path,
):
    store = Rig(tmp_path / "work.sqlite").open()
    a = capture(V)
    store.admit(a)
    store.resolve_observed_merge(
        record_id=a.evidence.record_id, merged_head_sha=L, observed_at=AT
    )
    before = store.record_for_id(a.evidence.record_id)
    store.resolve_observed_merge(
        record_id=a.evidence.record_id, merged_head_sha=L, observed_at=LATER
    )
    assert store.record_for_id(a.evidence.record_id) == before
