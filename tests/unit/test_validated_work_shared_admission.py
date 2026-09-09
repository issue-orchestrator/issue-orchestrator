"""Receipt-ranked admission and publication claims share one durable owner."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from issue_orchestrator.control.validated_work_admission import RankedEvidenceAdmission
from issue_orchestrator.domain.validated_work import ValidatedWorkState as State
from issue_orchestrator.domain.validated_work import ValidatedWorkFailure as Failure
from issue_orchestrator.domain.validated_work_store import (
    AdmissionStatus,
    EvidenceAdmissionSelection,
    EvidenceRole,
)
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.validated_work_ancestry import GitValidatedWorkAncestry
from issue_orchestrator.infra.validated_work_intake_store import SqliteValidatedWorkIntakeStore
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
from tests.unit.test_validated_work_preservation import custody, retained_receipt_pair
from tests.unit.validated_work_support import (
    LATER,
    Liveness,
    Rig,
    capture,
    changed_observations,
    claim,
)


@pytest.mark.parametrize("state", [State.QUEUED, State.PARKED, State.FAILED])
@pytest.mark.parametrize("admission_only", [False, True])
def test_admission_cannot_replace_or_refresh_a_claimed_resting_record(
    tmp_path, state, admission_only
):
    rig = Rig(tmp_path / "work.sqlite")
    recovery = rig.open()
    original = capture(
        state=state, failure=Failure.ARTIFACT_MISSING if state is State.FAILED else None,
    )
    recovery.admit(original)
    held = claim(recovery, original)
    before = recovery.record_for_id(held.record_id)
    reader = (
        SqliteValidatedWorkIntakeStore(rig.path, rig.graph, rig.artifacts)
        if admission_only else rig.open()
    )
    incoming = capture(run="newer", state=State.PARKED, at=LATER)
    result = reader.admit_selected(
        incoming, original.evidence.evidence_id, EvidenceAdmissionSelection.CURRENT
    )
    assert result is not None and result.status is AdmissionStatus.ATTACHED
    reader.admit(changed_observations(
        capture(state=State.PARKED), pr_number=200,
    ))
    assert recovery.record_for_id(held.record_id) == before
    assert recovery.holds_claim(held)
    assert recovery.attached_evidence(held.record_id)[0].evidence_id == incoming.evidence.evidence_id
    assert reader.evidence_for_id(incoming.evidence.evidence_id).evidence.role is EvidenceRole.ATTACHED


def test_rank_selection_cannot_override_a_claim_acquired_after_snapshot(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    admission = rig.open()
    recovery = rig.open()
    original = capture(state=State.PARKED)
    admission.admit(original)
    observed = admission.for_issue(6914).dispositions[0]
    held = claim(recovery, original)
    newer = capture(run="after-snapshot", state=State.PARKED, at=LATER)
    result = admission.admit_selected(
        newer, observed.evidence_id, EvidenceAdmissionSelection.CURRENT
    )
    assert result is not None and result.status is AdmissionStatus.ATTACHED
    assert recovery.get(held.record_id).evidence_id == observed.evidence_id
    assert recovery.holds_claim(held)


def test_current_compare_and_swap_refuses_without_partial_insert(tmp_path):
    store = Rig(tmp_path / "work.sqlite").open()
    first, rival, late = (capture(run=name) for name in ("first", "rival", "late"))
    store.admit(first)
    store.admit(rival)
    result = store.admit_selected(
        late, first.evidence.evidence_id, EvidenceAdmissionSelection.CURRENT
    )
    assert result is None
    assert store.evidence_for_id(late.evidence.evidence_id) is None
    assert store.get(first.evidence.record_id).evidence_id == rival.evidence.evidence_id


def full_receipt_store(custody, store_type=SqliteValidatedWorkStore):
    return store_type(
        custody.state / "shared.sqlite",
        ancestry=GitValidatedWorkAncestry(
            repository=custody.repo, repo_slug="owner/repo", git=custody.wc,
        ),
        artifacts=custody.escrow,
        retention=custody.escrow,
        liveness=Liveness(),
    )


def test_reopened_claim_store_preserves_global_receipt_order(custody):
    older, newer = retained_receipt_pair(custody)
    recovery = full_receipt_store(custody)
    ranked = RankedEvidenceAdmission(recovery, custody.ledger)
    ranked.admit(newer)
    ranked.admit(older)
    reopened = full_receipt_store(custody)
    assert reopened.get(newer.evidence.record_id).evidence_id == newer.evidence.evidence_id
    assert len(reopened.retained_evidence(42)) == 2
    held = claim(reopened, newer)
    assert recovery.holds_claim(held)
    assert ranked.for_issue(42) == reopened.for_issue(42)
    assert ranked.has_unresolved_work(42)


def test_two_ranked_claim_stores_rerank_after_concurrent_first_admission(custody):
    older, newer = retained_receipt_pair(custody)
    barrier = Barrier(2)

    class ConcurrentStore(SqliteValidatedWorkStore):
        def admit_selected(self, admission, expected_current, selection):
            if expected_current is None:
                barrier.wait(timeout=10)
            return super().admit_selected(admission, expected_current, selection)

    ranked = [RankedEvidenceAdmission(
        full_receipt_store(custody, ConcurrentStore),
        SqliteIssueRunLedger(custody.state / "runs.sqlite"),
    ) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda pair: pair[0].admit(pair[1]), zip(ranked, (newer, older)),
        ))
    assert len(results) == 2
    reopened = full_receipt_store(custody)
    assert reopened.get(newer.evidence.record_id).evidence_id == newer.evidence.evidence_id
    assert len(reopened.retained_evidence(42)) == 2
