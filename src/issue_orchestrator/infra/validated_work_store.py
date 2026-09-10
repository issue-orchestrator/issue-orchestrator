"""Repository-scoped transactional disposition store (design slices 1 and 1a).

No intake, external publication, lifecycle or UI runs here. Dependencies are
trusted constructor capabilities. Every multi-row policy remains inside the
store's own IMMEDIATE transaction; no caller can supply a transaction or a
freshness/death/containment boolean.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

from ..domain.retention_clock import retention_instant
from ..domain.recovery_block import RecoveryBlockSnapshot, RecoveryCleanupKey
from ..domain.published_work_finalization import FinalizationCheckpoint, PublishedWorkTarget

from ..domain.validated_work import (
    ResolutionKind,
    ValidatedWorkFailure,
    ValidatedWorkState,
    require_positive,
)
from ..domain.validated_work_claim import (
    ProcessIdentity,
    RetainedClaim,
    ValidatedWorkClaim,
)
from ..domain.validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
    ValidatedWorkDisposition,
    ValidatedWorkDispositionBatch,
)
from ..domain.validated_work_store import (
    AdmissionOutcome,
    DispositionPhase,
    EvidenceAdmission,
    EvidenceAdmissionSelection,
    EvidenceLookup,
    EvidenceRow,
    FinalizationPhase,
    LineagePublication,
    LineageResolutionRefusal,
    PublicationResolution,
    PublishAttempt,
    PublishValidatedHeadStatus,
    ValidatedWorkRecord,
)
from ..domain.validated_work_remote_authority import (
    RemoteAuthorityDecision,
    RemoteAuthorityRefreshRequest,
)
from ..ports.validated_work_verification import (
    OrchestratorLivenessPort,
    ValidatedWorkAncestry,
    ValidatedWorkArtifactVerifier,
)
from ..ports.validated_work_drain import ValidatedWorkDrainRequest
from ..ports.validated_work_escrow import EvidenceReleaser
from .validated_work_admission import EvidenceAdmissionWriter
from .validated_work_snapshots import DispositionSnapshots
from .validated_work_recovery_blocks import RecoveryBlockPersistence
from .validated_work_attempts import PublishAttemptWriter
from .validated_work_claims import ClaimAuthority, owner_identity
from .validated_work_lineage import LineageClassifier
from .validated_work_resolution import PublicationResolver
from .validated_work_rows import (
    DispositionDatabase,
    attempt_row,
    current_evidence,
    disposition,
    evidence_row,
    publication,
    record_row,
    refresh_observations,
    retention_evidence_row,
)


class SqliteValidatedWorkStore:
    def __init__(
        self,
        db_path: Path,
        *,
        ancestry: ValidatedWorkAncestry,
        artifacts: ValidatedWorkArtifactVerifier,
        liveness: OrchestratorLivenessPort,
        retention: EvidenceReleaser,
    ) -> None:
        if ancestry is None or artifacts is None or liveness is None or retention is None:
            raise ValueError(
                "ancestry, artifact verification, liveness and retention are required capabilities"
            )
        self._retention = retention
        self._db = DispositionDatabase(db_path)
        self._snapshots = DispositionSnapshots(self._db)
        self._recovery_blocks = RecoveryBlockPersistence(self._db)
        self._lineage = LineageClassifier(ancestry, artifacts)
        self._claims = ClaimAuthority(liveness)
        self._admission = EvidenceAdmissionWriter(self._lineage)
        self._attempts = PublishAttemptWriter(self._claims, self._lineage)
        self._resolution = PublicationResolver(self._claims, self._lineage)

    def admit(self, admission: EvidenceAdmission) -> AdmissionOutcome:
        with self._db.transaction(write=True) as conn:
            status = self._admission.admit(conn, admission)
            return AdmissionOutcome(
                status, disposition(conn, admission.evidence.record_id)
            )

    def admit_selected(
        self,
        admission: EvidenceAdmission,
        expected_current: str | None,
        selection: EvidenceAdmissionSelection,
    ) -> AdmissionOutcome | None:
        """Apply the receipt owner's selection in the claim store transaction."""
        with self._db.transaction(write=True) as conn:
            status = self._admission.admit_selected(
                conn, admission, expected_current, selection
            )
            return (
                None
                if status is None
                else AdmissionOutcome(
                    status, disposition(conn, admission.evidence.record_id)
                )
            )

    def drain_requests(
        self, *, after_record_id: str, limit: int
    ) -> tuple[ValidatedWorkDrainRequest, ...]:
        return self._snapshots.drain_requests(after_record_id=after_record_id, limit=limit)

    def get(self, record_id: str) -> ValidatedWorkDisposition:
        with self._db.transaction() as conn:
            return disposition(conn, record_id)

    def record_for_id(self, record_id: str) -> ValidatedWorkRecord:
        with self._db.transaction() as conn:
            return self._record_for_id(conn, record_id)

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        return self._snapshots.for_issue(issue_number)

    def recovery_block_snapshot(self, repo_slug: str, issue_number: int) -> RecoveryBlockSnapshot:
        return self._recovery_blocks.snapshot(repo_slug, issue_number)

    def begin_block_label_cleanup(
        self, keys: tuple[RecoveryCleanupKey, ...], label: str
    ) -> bool:
        return self._recovery_blocks.begin_label_cleanup(keys, label)

    def acknowledge_block_cleanup(self, keys: tuple[RecoveryCleanupKey, ...]) -> bool:
        return self._recovery_blocks.acknowledge(keys)

    def has_unresolved_work(self, issue_number: int) -> bool:
        return self._snapshots.has_unresolved_work(issue_number)

    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None:
        return self._snapshots.evidence_for_id(evidence_id)

    def attached_evidence(self, record_id: str) -> tuple[EvidenceRow, ...]:
        with self._db.transaction() as conn:
            return tuple(
                evidence_row(row)
                for row in conn.execute(
                    "SELECT * FROM validated_work_evidence WHERE record_id=? AND role='attached' ORDER BY admitted_at,evidence_id",
                    (record_id,),
                )
            )

    def evidence_for_retention(
        self, *, released_before: str
    ) -> tuple[EvidenceRow, ...]:
        cutoff = retention_instant(released_before)
        with self._db.transaction() as conn:
            return tuple(
                evidence
                for row in conn.execute(
                    "SELECT e.* FROM validated_work_evidence e JOIN validated_work_records r USING(record_id) "
                    "WHERE r.state IN ('recovered','abandoned') "
                    "AND e.released_at='' ORDER BY e.admitted_at,e.evidence_id",
                )
                if (evidence := retention_evidence_row(conn, row, cutoff)) is not None
            )

    def release_evidence_for_retention(
        self, evidence_id: str, *, released_before: str, released_at: str,
    ) -> bool:
        # The write transaction is the admission/cleanup serialization boundary.
        # An unresolved or still-owned record can never reach the filesystem call.
        cutoff = retention_instant(released_before)
        retention_instant(released_at)
        with self._db.transaction(write=True) as conn:
            row = conn.execute(
                "SELECT e.* FROM validated_work_evidence e JOIN validated_work_records r USING(record_id) "
                "WHERE e.evidence_id=? AND r.state IN ('recovered','abandoned') "
                "AND e.released_at='' "
                "AND r.owner_claim_hash='' AND r.stop_reservation_id=''",
                (evidence_id,),
            ).fetchone()
            if row is None:
                return False
            evidence = retention_evidence_row(conn, row, cutoff)
            if evidence is None:
                return False
            self._retention.release(evidence)
            conn.execute("UPDATE validated_work_evidence SET released_at=? WHERE evidence_id=?", (released_at, evidence_id))
            return True

    def lineage_publication(self, lineage_key: str) -> LineagePublication | None:
        with self._db.transaction() as conn:
            return publication(conn, lineage_key)

    def acquire_claim(
        self,
        record_id: str,
        *,
        expected_states: frozenset[ValidatedWorkState],
        evidence_id: str,
    ) -> ValidatedWorkClaim | None:
        with self._db.transaction(write=True) as conn:
            return self._claims.acquire(conn, record_id, expected_states, evidence_id)

    def holds_claim(self, claim: ValidatedWorkClaim) -> bool:
        with self._db.transaction() as conn:
            return self._claims.holds(conn, claim)

    def relinquish_claim(self, claim: ValidatedWorkClaim) -> bool:
        with self._db.transaction(write=True) as conn:
            return self._claims.relinquish(conn, claim)

    def owner_of(self, record_id: str) -> ProcessIdentity | None:
        with self._db.transaction() as conn:
            return owner_identity(record_row(conn, record_id))

    def retained_claims(
        self, states: frozenset[ValidatedWorkState]
    ) -> tuple[RetainedClaim, ...]:
        with self._db.transaction() as conn:
            candidates = []
            for row in conn.execute(
                "SELECT * FROM validated_work_records WHERE owner_claim_hash!='' ORDER BY record_id"
            ):
                if row["state"] in states:
                    owner = owner_identity(row)
                    assert owner is not None
                    candidates.append(
                        RetainedClaim(
                            row["record_id"],
                            current_evidence(conn, row["record_id"]).evidence_id,
                            ValidatedWorkState(row["state"]),
                            owner,
                        )
                    )
            return tuple(candidates)

    def refresh_remote_authority(
        self,
        claim: ValidatedWorkClaim,
        request: RemoteAuthorityRefreshRequest,
        decision: RemoteAuthorityDecision,
        *,
        refreshed_at: str,
    ) -> ValidatedWorkDisposition | None:
        with self._db.transaction(write=True) as conn:
            if not self._claims.holds(conn, claim):
                return None
            record = self._record_for_id(conn, claim.record_id)
            if request.refusal(record) is not None:
                return None
            decision.require_preserved_capture(record)
            evidence = record.current_evidence
            refresh_observations(conn, evidence.evidence_id, decision.observations)
            conn.execute(
                "UPDATE validated_work_evidence SET base_state=?,base_failure=?,base_reason=? "
                "WHERE evidence_id=?",
                (
                    decision.state.value,
                    decision.failure.value if decision.failure else "",
                    decision.reason,
                    evidence.evidence_id,
                ),
            )
            if (
                record.disposition.state is ValidatedWorkState.PUBLISHING
                and decision.state is ValidatedWorkState.QUEUED
            ):
                conn.execute(
                    "UPDATE validated_work_records SET failure='',reason=?,updated_at=? "
                    "WHERE record_id=? AND owner_fence=?",
                    (decision.reason, refreshed_at, claim.record_id, claim.fence),
                )
            else:
                # Vacate the drainable slot before lineage atomically installs
                # the refreshed base gate and its relationships.
                conn.execute(
                    "UPDATE validated_work_records SET state='parked',failure=?,reason=?,updated_at=? "
                    "WHERE record_id=? AND owner_fence=?",
                    (
                        decision.failure.value if decision.failure else "",
                        decision.reason,
                        refreshed_at,
                        claim.record_id,
                        claim.fence,
                    ),
                )
                self._lineage.classify(
                    conn,
                    record.lineage_key,
                    refreshed_at,
                    reconsider=frozenset({claim.record_id}),
                )
            return disposition(conn, claim.record_id)

    @staticmethod
    def _record_for_id(
        conn: sqlite3.Connection, record_id: str
    ) -> ValidatedWorkRecord:
        row = record_row(conn, record_id)
        return ValidatedWorkRecord(
            disposition(conn, record_id),
            current_evidence(conn, record_id),
            row["lineage_key"],
            row["superseded_by_record_id"],
            row["waits_on_record_id"],
            row["owner_fence"],
            owner_identity(row),
            FinalizationPhase(row["finalization_phase"]),
            row["publishing_started_at"],
            ResolutionKind(row["resolution_kind"])
            if row["resolution_kind"]
            else None,
            row["resolved_at"],
            row["created_at"],
            row["updated_at"],
            row["terminal_at"],
        )

    def resolve_attached_evidence(
        self, claim: ValidatedWorkClaim, *, record_id: str, resolved_at: str
    ) -> ValidatedWorkDisposition | None:
        with self._db.transaction(write=True) as conn:
            if record_id != claim.record_id or not self._claims.holds(conn, claim):
                return None
            self._admission.resolve_attached(conn, record_id, resolved_at)
            return disposition(conn, record_id)

    def begin_publish_attempt(
        self,
        claim: ValidatedWorkClaim,
        *,
        expected_attempt_no: int,
        target_head_sha: str,
        expected_remote_head: str,
        phase: DispositionPhase,
        started_at: str,
        authority: ValidatedWorkAuthoritySnapshot | None = None,
    ) -> PublishAttempt | None:
        with self._db.transaction(write=True) as conn:
            return self._attempts.begin(
                conn,
                claim,
                expected_attempt_no=expected_attempt_no,
                target_head_sha=target_head_sha,
                expected_remote_head=expected_remote_head,
                phase=phase,
                started_at=started_at,
                authority=authority,
            )

    def publish_attempts(self, record_id: str) -> tuple[PublishAttempt, ...]:
        with self._db.transaction() as conn:
            return tuple(
                attempt_row(row)
                for row in conn.execute(
                    "SELECT * FROM validated_work_publish_attempts WHERE record_id=? ORDER BY attempt_no",
                    (record_id,),
                )
            )

    def record_attempt_outcome(
        self,
        claim: ValidatedWorkClaim,
        attempt: PublishAttempt,
        *,
        outcome: PublishValidatedHeadStatus,
        failure: ValidatedWorkFailure | None,
        finished_at: str,
    ) -> bool:
        with self._db.transaction(write=True) as conn:
            return self._attempts.outcome(
                conn,
                claim,
                attempt,
                outcome=outcome,
                failure=failure,
                finished_at=finished_at,
            )

    def read_finalization_checkpoint(
        self, claim: ValidatedWorkClaim, target: PublishedWorkTarget
    ) -> FinalizationCheckpoint | None:
        with self._db.transaction() as conn:
            return self._attempts.read_finalization(conn, claim, target)

    def record_finalization_phase(
        self, claim: ValidatedWorkClaim, *, phase: FinalizationPhase, recorded_at: str
    ) -> bool:
        with self._db.transaction(write=True) as conn:
            return self._attempts.finalization(conn, claim, phase, recorded_at)

    def finalization_phase(self, record_id: str) -> FinalizationPhase:
        with self._db.transaction() as conn:
            return FinalizationPhase(record_row(conn, record_id)["finalization_phase"])

    def record_pr_number(self, claim: ValidatedWorkClaim, *, pr_number: int) -> bool:
        require_positive(pr_number, "pr_number")
        with self._db.transaction(write=True) as conn:
            if (
                not self._claims.holds(conn, claim)
                or record_row(conn, claim.record_id)["state"] != "publishing"
            ):
                return False
            evidence = current_evidence(conn, claim.record_id)
            refresh_observations(
                conn,
                evidence.evidence_id,
                replace(evidence.admission.evidence.observations, pr_number=pr_number),
            )
            return True

    def fail(
        self,
        claim: ValidatedWorkClaim,
        *,
        failure: ValidatedWorkFailure,
        reason: str,
        failed_at: str,
    ) -> bool:
        with self._db.transaction(write=True) as conn:
            return self._attempts.fail(conn, claim, failure, reason, failed_at)

    def resolve_published(
        self,
        claim: ValidatedWorkClaim,
        *,
        record_id: str,
        published_head_sha: str,
        pre_push_expected: str,
        finalized_at: str,
    ) -> PublicationResolution | LineageResolutionRefusal:
        with self._db.transaction(write=True) as conn:
            return self._resolution.published(
                conn,
                claim,
                record_id=record_id,
                published_head_sha=published_head_sha,
                pre_push_expected=pre_push_expected,
                finalized_at=finalized_at,
            )

    def resolve_observed_merge(
        self, *, record_id: str, merged_head_sha: str, observed_at: str
    ) -> PublicationResolution | LineageResolutionRefusal:
        with self._db.transaction(write=True) as conn:
            return self._resolution.observed_merge(
                conn,
                record_id=record_id,
                merged_head_sha=merged_head_sha,
                observed_at=observed_at,
            )

    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]:
        return self._snapshots.retained_evidence(issue_number)
