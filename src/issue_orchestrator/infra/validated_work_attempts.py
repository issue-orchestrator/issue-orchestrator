"""Append-before-invoke attempts and fenced, monotonic publication progress."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from ..domain.validated_work import (
    LineageRole,
    ValidatedWorkState,
    ValidatedWorkFailure as Failure,
    require_positive,
    require_sha,
)
from ..domain.published_work_finalization import FinalizationCheckpoint, PublishedWorkTarget
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
from ..domain.validated_work_publish_policy import (
    publication_eligible,
    retry_permitted,
    recordable_outcome,
    attempt_requires_failure,
)
from .validated_work_claims import ClaimAuthority
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import (
    attempt_row,
    current_evidence,
    has_successful_attempt,
    latest_attempt,
    record_row,
)

PUBLISH_ATTEMPT_LIMIT = 5


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
        if not publication_eligible(
            state=ValidatedWorkState(row["state"]),
            lineage_role=LineageRole(row["lineage_role"]),
            current=evidence.authority,
            approved=authority,
            target=target_head_sha,
            expected=expected_remote_head,
            phase=phase,
        ):
            return None
        last = latest_attempt(conn, claim.record_id)
        count = last.attempt_no if last is not None else 0
        if count != expected_attempt_no or not retry_permitted(
            last,
            fence=claim.fence,
            evidence_id=evidence.evidence_id,
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
        if (
            not self._claims.holds(conn, claim)
            or type(attempt) is not PublishAttempt
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
        if not recordable_outcome(outcome):
            return False
        # The same constructor that validates durable reads must admit the
        # completed value before any write. Do not duplicate its shape rules.
        completed = replace(
            attempt,
            outcome=outcome,
            failure=failure,
            finished_at=finished_at,
        )
        conn.execute(
            "UPDATE validated_work_publish_attempts SET outcome=?,failure=?,finished_at=? WHERE record_id=? AND attempt_no=? AND outcome='' AND fence=?",
            (
                outcome.value,
                failure.value if failure else "",
                completed.finished_at,
                claim.record_id,
                attempt.attempt_no,
                claim.fence,
            ),
        )
        if attempt_requires_failure(
            outcome, attempt_no=attempt.attempt_no, limit=PUBLISH_ATTEMPT_LIMIT
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

    def read_finalization(
        self, conn: sqlite3.Connection, claim: ValidatedWorkClaim,
        target: PublishedWorkTarget,
    ) -> FinalizationCheckpoint | None:
        if not self._claims.holds(conn, claim) or claim.record_id != target.key.record_id:
            return None
        row = record_row(conn, claim.record_id)
        evidence = current_evidence(conn, claim.record_id)
        identity = evidence.admission.evidence.identity
        if (
            identity.key != target.key
            or identity.review_disposition is not target.review_disposition
            or evidence.admission.evidence.observations.pr_number != target.pr_number
            or row["state"] not in {"publishing", "recovered", "failed"}
            or not has_successful_attempt(conn, claim.record_id)
        ):
            return None
        phase = FinalizationPhase(row["finalization_phase"])
        if row["state"] == "recovered" and phase is not FinalizationPhase.COMPLETE:
            return None
        if row["state"] == "publishing" and phase is FinalizationPhase.COMPLETE:
            return None
        if row["state"] == "failed":
            if row["failure"] != Failure.REVIEW_ROUTING_FAILED.value or phase is not FinalizationPhase.NOT_STARTED:
                return None
            return FinalizationCheckpoint(phase, Failure.REVIEW_ROUTING_FAILED, row["reason"])
        return FinalizationCheckpoint(phase, None, "publication finalization admitted")

    def finalization(
        self,
        conn: sqlite3.Connection,
        claim: ValidatedWorkClaim,
        phase: FinalizationPhase,
        at: str,
    ) -> bool:
        if not self._claims.holds(conn, claim):
            return False
        if not isinstance(phase, FinalizationPhase):
            raise ValueError("finalization phase must be typed")
        row = record_row(conn, claim.record_id)
        if row["state"] != "publishing" or not has_successful_attempt(
            conn, claim.record_id
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
