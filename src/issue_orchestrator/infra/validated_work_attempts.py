"""Append-before-invoke attempts and fenced, monotonic publication progress."""

from __future__ import annotations

import sqlite3

from ..domain.validated_work import (
    LineageRole,
    ValidatedWorkFailure as Failure,
    require_positive,
    require_sha,
)
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
from ..domain.validated_work_store import (
    AncestryRelation,
    CommitReference,
    DispositionPhase,
    FinalizationPhase,
    PublishAttempt,
    PublishValidatedHeadStatus as Status,
)
from .validated_work_claims import ClaimAuthority
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import attempt_row, current_evidence, record_row

PUBLISH_ATTEMPT_LIMIT = 5
SUCCESS_STATUSES = frozenset({Status.PUBLISHED, Status.ALREADY_AT_TARGET})


class PublishAttemptWriter:
    def __init__(self, claims: ClaimAuthority, lineage: LineageClassifier) -> None:
        self._claims = claims
        self._lineage = lineage

    def begin(
        self,
        conn: sqlite3.Connection,
        claim: ValidatedWorkClaim,
        *,
        expected_attempt_no: int,
        target_head_sha: str,
        expected_remote_head: str,
        phase: DispositionPhase,
        started_at: str,
        authority: ValidatedWorkAuthoritySnapshot | None,
    ) -> PublishAttempt | None:
        require_positive(expected_attempt_no, "expected_attempt_no", minimum=0)
        require_sha(target_head_sha)
        if expected_remote_head:
            require_sha(expected_remote_head)
        if not self._claims.holds(conn, claim):
            return None
        row = record_row(conn, claim.record_id)
        evidence = current_evidence(conn, claim.record_id)
        if not self._eligible(
            row,
            evidence.authority,
            authority,
            target_head_sha,
            expected_remote_head,
            phase,
        ):
            return None
        last = conn.execute(
            "SELECT * FROM validated_work_publish_attempts WHERE record_id=? ORDER BY attempt_no DESC LIMIT 1",
            (claim.record_id,),
        ).fetchone()
        count = last["attempt_no"] if last is not None else 0
        if count != expected_attempt_no or not self._retry_permitted(
            last, claim, evidence.evidence_id
        ):
            return None
        if count >= PUBLISH_ATTEMPT_LIMIT:
            self.fail(
                conn,
                claim,
                Failure.PUSH_FAILED,
                "publish attempt limit exhausted",
                started_at,
            )
            return None
        reference = CommitReference(
            evidence.admission.evidence.identity.key, evidence.admission.pinned_ref
        )
        if self._lineage.compare(
            reference, reference
        ) is not AncestryRelation.EQUAL or not self._lineage.verifies(evidence):
            return None
        if not self._lineage.select_for_publication(conn, claim.record_id, started_at):
            return None
        attempt = PublishAttempt(
            claim.record_id,
            count + 1,
            evidence.evidence_id,
            target_head_sha,
            expected_remote_head,
            phase,
            claim.fence,
            started_at,
        )
        conn.execute(
            "INSERT INTO validated_work_publish_attempts (record_id,attempt_no,evidence_id,target_head_sha,expected_remote_head,phase,fence,started_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                attempt.record_id,
                attempt.attempt_no,
                attempt.evidence_id,
                target_head_sha,
                expected_remote_head,
                phase.value,
                claim.fence,
                started_at,
            ),
        )
        conn.execute(
            "UPDATE validated_work_records SET state='publishing',failure='',reason='',publishing_started_at=CASE WHEN publishing_started_at='' THEN ? ELSE publishing_started_at END,updated_at=? WHERE record_id=? AND owner_fence=?",
            (started_at, started_at, claim.record_id, claim.fence),
        )
        return attempt

    @staticmethod
    def _eligible(
        row: sqlite3.Row,
        current: ValidatedWorkAuthoritySnapshot,
        approved: ValidatedWorkAuthoritySnapshot | None,
        target: str,
        expected: str,
        phase: DispositionPhase,
    ) -> bool:
        if approved is not None and approved != current:
            return False
        if target != current.validated_head_sha or expected != (
            current.expected_remote_head_sha or ""
        ):
            return False
        if row["state"] == "publishing":
            return phase is DispositionPhase.RECONCILING
        if (
            phase is not DispositionPhase.PRE_SUBMISSION
            or row["lineage_role"] == LineageRole.PENDING
        ):
            return False
        if row["state"] == "queued":
            return row["lineage_role"] == LineageRole.HEAD
        # A parked head requires exact snapshot consent; an arbitrary state set
        # passed to acquire_claim is never approval to publish it.
        return row["state"] == "parked" and approved == current

    @staticmethod
    def _retry_permitted(
        last: sqlite3.Row | None, claim: ValidatedWorkClaim, evidence_id: str
    ) -> bool:
        if last is None:
            return True
        if last["evidence_id"] != evidence_id and last["outcome"]:
            return True  # new capture gets its own attempt; history/budget remain
        if last["outcome"] in SUCCESS_STATUSES:
            return False  # resume finalization, never repeat a successful push
        if not last["outcome"]:
            return last["fence"] != claim.fence  # crash reconciliation by successor
        return last["outcome"] == Status.TRANSIENT_FAILURE

    def outcome(
        self,
        conn: sqlite3.Connection,
        claim: ValidatedWorkClaim,
        attempt: PublishAttempt,
        *,
        outcome: Status,
        failure: Failure | None,
        finished_at: str,
    ) -> bool:
        if not isinstance(outcome, Status) or (
            failure is not None and not isinstance(failure, Failure)
        ):
            raise ValueError("attempt outcome and failure must be typed")
        if outcome is Status.SUPERSEDED:
            return False
        if outcome not in SUCCESS_STATUSES and failure is None:
            raise ValueError("unsuccessful attempt requires enumerated failure")
        if outcome in SUCCESS_STATUSES and failure is not None:
            raise ValueError("successful attempt cannot carry failure")
        if (
            not self._claims.holds(conn, claim)
            or attempt.record_id != claim.record_id
            or attempt.fence != claim.fence
        ):
            return False
        stored = conn.execute(
            "SELECT * FROM validated_work_publish_attempts WHERE record_id=? AND attempt_no=?",
            (claim.record_id, attempt.attempt_no),
        ).fetchone()
        if (
            stored is None
            or attempt_row(stored) != attempt
            or attempt.outcome is not None
        ):
            return False
        if record_row(conn, claim.record_id)["state"] != "publishing":
            return False
        conn.execute(
            "UPDATE validated_work_publish_attempts SET outcome=?,failure=?,finished_at=? WHERE record_id=? AND attempt_no=? AND outcome='' AND fence=?",
            (
                outcome.value,
                failure.value if failure else "",
                finished_at,
                claim.record_id,
                attempt.attempt_no,
                claim.fence,
            ),
        )
        if outcome in {Status.REJECTED, Status.DIVERGED} or (
            outcome is Status.TRANSIENT_FAILURE
            and attempt.attempt_no >= PUBLISH_ATTEMPT_LIMIT
        ):
            assert failure is not None
            self.fail(conn, claim, failure, failure.value, finished_at)
        return True

    def fail(
        self,
        conn: sqlite3.Connection,
        claim: ValidatedWorkClaim,
        failure: Failure,
        reason: str,
        at: str,
    ) -> bool:
        if not isinstance(failure, Failure):
            raise ValueError("failure must be typed")
        if not self._claims.holds(conn, claim):
            return False
        row = record_row(conn, claim.record_id)
        if row["state"] in {"recovered", "abandoned"}:
            return False
        conn.execute(
            "UPDATE validated_work_records SET state='failed',failure=?,reason=?,updated_at=? WHERE record_id=? AND owner_fence=?",
            (failure.value, reason, at, claim.record_id, claim.fence),
        )
        self._lineage.classify(conn, row["lineage_key"], at)
        return True

    def finalization(
        self,
        conn: sqlite3.Connection,
        claim: ValidatedWorkClaim,
        phase: FinalizationPhase,
        at: str,
    ) -> bool:
        if not isinstance(phase, FinalizationPhase):
            raise ValueError("finalization phase must be typed")
        if not self._claims.holds(conn, claim):
            return False
        row = record_row(conn, claim.record_id)
        last = conn.execute(
            "SELECT outcome FROM validated_work_publish_attempts WHERE record_id=? ORDER BY attempt_no DESC LIMIT 1",
            (claim.record_id,),
        ).fetchone()
        if (
            row["state"] != "publishing"
            or last is None
            or last["outcome"] not in SUCCESS_STATUSES
        ):
            return False
        old = FinalizationPhase(row["finalization_phase"])
        if phase is old:
            return True
        next_phase = {
            FinalizationPhase.NOT_STARTED: FinalizationPhase.REVIEW_ROUTED,
            FinalizationPhase.REVIEW_ROUTED: FinalizationPhase.RECOVERY_CLEARED,
        }
        if next_phase.get(old) is not phase:
            return False  # COMPLETE is inseparable from resolve_published
        conn.execute(
            "UPDATE validated_work_records SET finalization_phase=?,updated_at=? WHERE record_id=? AND owner_fence=?",
            (phase.value, at, claim.record_id, claim.fence),
        )
        return True
