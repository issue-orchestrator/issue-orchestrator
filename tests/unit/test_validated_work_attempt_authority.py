"""Durable attempt corruption must not become publication or retention authority."""

from contextlib import closing
import hashlib
import sqlite3

import pytest

from issue_orchestrator.domain.validated_work import ValidatedWorkState as State
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
    Liveness,
    Rig,
    begin,
    capture,
    claim,
)


def _snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        return hashlib.sha256("\n".join(conn.iterdump()).encode()).digest()


def _checkpoint(store, token, attempt, phase, outcome=Status.PUBLISHED):
    assert store.record_attempt_outcome(
        token, attempt, outcome=outcome, failure=None, finished_at=LATER
    )
    if phase is not Phase.NOT_STARTED:
        assert store.record_finalization_phase(
            token, phase=Phase.REVIEW_ROUTED, recorded_at=LATER
        )
    if phase is Phase.RECOVERY_CLEARED:
        assert store.record_finalization_phase(
            token, phase=Phase.RECOVERY_CLEARED, recorded_at=LATER
        )


def _corrupt(path, corruption):
    statements = {
        "unfinished": ("UPDATE validated_work_publish_attempts SET finished_at=''", ()),
        "failure": (
            "UPDATE validated_work_publish_attempts SET failure='push_failed'",
            (),
        ),
        "target": (
            "UPDATE validated_work_publish_attempts SET target_head_sha='not-a-sha'",
            (),
        ),
        "other_target": (
            "UPDATE validated_work_publish_attempts SET target_head_sha=?",
            (L,),
        ),
        "other_baseline": (
            "UPDATE validated_work_publish_attempts SET expected_remote_head=?",
            (L,),
        ),
        "other_evidence": (
            "UPDATE validated_work_publish_attempts SET evidence_id=?",
            (capture(L).evidence.evidence_id,),
        ),
    }
    with closing(sqlite3.connect(path)) as conn, conn:
        statement, values = statements[corruption]
        conn.execute(statement, values)


def _resolve(store, token):
    return store.resolve_published(
        token,
        record_id=token.record_id,
        published_head_sha=V,
        pre_push_expected=ROOT,
        finalized_at=LATER,
    )


@pytest.mark.parametrize(
    "corruption,malformed",
    [
        ("unfinished", True),
        ("failure", True),
        ("target", True),
        ("other_target", False),
        ("other_baseline", False),
        ("other_evidence", False),
    ],
)
@pytest.mark.parametrize(
    "checkpoint", [Phase.NOT_STARTED, Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED]
)
def test_corrupt_success_cannot_advance_phase_or_resolve_after_reopen(
    tmp_path, corruption, malformed, checkpoint
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    token = claim(store, admission)
    attempt = begin(store, token)
    # Resolution would touch subject, ancestor, waiter and publication fact.
    store.admit(capture(ROOT))
    store.admit(capture(L))
    _checkpoint(store, token, attempt, checkpoint)
    _corrupt(rig.path, corruption)
    before = _snapshot(rig.path)
    for current in (store, rig.open()):
        if malformed:
            with pytest.raises(ValueError):
                current.publish_attempts(token.record_id)
        else:
            assert current.publish_attempts(token.record_id)[0].succeeded
        if checkpoint is Phase.RECOVERY_CLEARED:
            with pytest.raises(ValueError):
                _resolve(current, token)
        else:
            next_phase = (
                Phase.REVIEW_ROUTED
                if checkpoint is Phase.NOT_STARTED
                else Phase.RECOVERY_CLEARED
            )
            with pytest.raises(ValueError):
                current.record_finalization_phase(
                    token, phase=next_phase, recorded_at=LATER
                )
        # Replaying an already recorded phase also revalidates the attempt.
        with pytest.raises(ValueError):
            current.record_finalization_phase(
                token, phase=checkpoint, recorded_at=LATER
            )
        assert _snapshot(rig.path) == before
        assert current.get(token.record_id).state is State.PUBLISHING
        assert current.has_unresolved_work(6914)
        assert current.evidence_for_retention(released_before="9999-01-01T00:00:00+00:00") == ()


@pytest.mark.parametrize(
    "corruption",
    [
        "unfinished",
        "failure",
        "target",
        "other_target",
        "other_baseline",
        "other_evidence",
    ],
)
def test_stale_owner_is_refused_before_corrupt_attempt_is_read(tmp_path, corruption):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    old = claim(store, admission)
    attempt = begin(store, old)
    _checkpoint(store, old, attempt, Phase.RECOVERY_CLEARED)
    successor = rig.open(Liveness(OTHER, dead={OWNER}))
    new = claim(successor, admission)
    assert new.fence > old.fence
    _corrupt(rig.path, corruption)
    before = _snapshot(rig.path)
    for current in (store, successor):
        for phase in (Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED):
            assert not current.record_finalization_phase(
                old, phase=phase, recorded_at=LATER
            )
        assert _resolve(current, old) is Refusal.STALE_CLAIM
        assert not current.record_attempt_outcome(
            old, attempt, outcome=Status.PUBLISHED, failure=None, finished_at=LATER
        )
        assert (
            current.begin_publish_attempt(
                old,
                expected_attempt_no=1,
                target_head_sha=V,
                expected_remote_head=ROOT,
                phase=DispositionPhase.RECONCILING,
                started_at=LATER,
            )
            is None
        )
        assert _snapshot(rig.path) == before
    with pytest.raises(ValueError):
        _resolve(successor, new)
    assert _snapshot(rig.path) == before


@pytest.mark.parametrize("outcome", [Status.PUBLISHED, Status.ALREADY_AT_TARGET])
@pytest.mark.parametrize(
    "checkpoint", [Phase.NOT_STARTED, Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED]
)
def test_successor_resumes_valid_success_without_original_attempt_fence(
    tmp_path, outcome, checkpoint
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    old = claim(store, admission)
    attempt = begin(store, old)
    _checkpoint(store, old, attempt, checkpoint, outcome)
    successor = rig.open(Liveness(OTHER, dead={OWNER}))
    new = claim(successor, admission)
    assert new.fence > attempt.fence
    assert begin(successor, new) is None
    for phase in (Phase.REVIEW_ROUTED, Phase.RECOVERY_CLEARED):
        assert not store.record_finalization_phase(old, phase=phase, recorded_at=LATER)
    assert _resolve(store, old) is Refusal.STALE_CLAIM
    if checkpoint is Phase.NOT_STARTED:
        assert successor.record_finalization_phase(
            new, phase=Phase.REVIEW_ROUTED, recorded_at=LATER
        )
    assert successor.record_finalization_phase(
        new, phase=Phase.RECOVERY_CLEARED, recorded_at=LATER
    )
    result = _resolve(successor, new)
    assert result.record.state is State.RECOVERED
    assert successor.publish_attempts(new.record_id)[0].fence == attempt.fence
    assert len(successor.publish_attempts(new.record_id)) == 1
    assert not successor.has_unresolved_work(6914)


@pytest.mark.parametrize("operation", ["retry", "outcome"])
def test_other_attempt_authorization_reads_validate_complete_row(tmp_path, operation):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    token = claim(store, admission)
    attempt = begin(store, token)
    _corrupt(rig.path, "target")
    before = _snapshot(rig.path)
    with pytest.raises(ValueError):
        if operation == "retry":
            store.begin_publish_attempt(
                token,
                expected_attempt_no=1,
                target_head_sha=V,
                expected_remote_head=ROOT,
                phase=DispositionPhase.RECONCILING,
                started_at=AT,
            )
        else:
            store.record_attempt_outcome(
                token,
                attempt,
                outcome=Status.PUBLISHED,
                failure=None,
                finished_at=LATER,
            )
    assert _snapshot(rig.path) == before


@pytest.mark.parametrize("attempt_present", [False, True])
def test_phase_checkpoint_without_successful_attempt_cannot_resolve(
    tmp_path, attempt_present
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    admission = capture()
    store.admit(admission)
    token = claim(store, admission)
    if attempt_present:
        begin(store, token)
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET state='publishing', finalization_phase='recovery_cleared'"
        )
    before = _snapshot(rig.path)
    reopened = rig.open()
    assert not reopened.record_finalization_phase(
        token, phase=Phase.RECOVERY_CLEARED, recorded_at=LATER
    )
    assert _resolve(reopened, token) is Refusal.FINALIZATION_INCOMPLETE
    assert _snapshot(rig.path) == before
