"""Atomic operator abandonment of one exact validated-work authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ..domain.retention_clock import retention_instant
from ..domain.validated_work import (
    EvidenceRole,
    ResolutionKind,
    ValidatedWorkState,
    canonical_json,
)
from ..domain.validated_work_commands import (
    AbandonStatus,
    AbandonValidatedWorkCommand,
    AbandonValidatedWorkOutcome,
    ValidatedWorkAuthoritySnapshot,
)
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import (
    DispositionDatabase,
    current_evidence,
    disposition,
)


class ValidatedWorkAbandonment:
    """Own authority comparison and resolution in one SQLite transaction."""

    def __init__(
        self,
        database: DispositionDatabase,
        lineage: LineageClassifier,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db = database
        self._lineage = lineage
        self._clock = clock

    def abandon_if_current(
        self, command: AbandonValidatedWorkCommand
    ) -> AbandonValidatedWorkOutcome:
        if type(command) is not AbandonValidatedWorkCommand:
            raise ValueError("abandonment store requires a typed command")
        with self._db.transaction(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM validated_work_records WHERE record_id=?",
                (command.authority.record_id,),
            ).fetchone()
            if row is None:
                return _refusal(
                    AbandonStatus.NO_SUCH_RECORD,
                    "The retained-work record no longer exists",
                )

            current = current_evidence(conn, row["record_id"])
            authority = current.authority
            evidence = conn.execute(
                "SELECT record_id,role FROM validated_work_evidence WHERE evidence_id=?",
                (command.authority.evidence_id,),
            ).fetchone()
            if evidence is None or evidence["record_id"] != row["record_id"]:
                return _stale(
                    AbandonStatus.AUTHORITY_STALE,
                    authority,
                    "The approved evidence does not belong to this current record",
                )
            if EvidenceRole(evidence["role"]) is not EvidenceRole.CURRENT:
                return _stale(
                    AbandonStatus.EVIDENCE_NOT_CURRENT,
                    authority,
                    "The approved evidence is no longer current",
                )
            if command.authority != authority:
                return _stale(
                    AbandonStatus.AUTHORITY_STALE,
                    authority,
                    "The retained-work authority changed after confirmation",
                )

            state = ValidatedWorkState(row["state"])
            if state in {ValidatedWorkState.RECOVERED, ValidatedWorkState.ABANDONED}:
                return _refusal(
                    AbandonStatus.ALREADY_RESOLVED,
                    "The retained-work record is already resolved",
                )
            if (
                state not in {ValidatedWorkState.PARKED, ValidatedWorkState.FAILED}
                or row["owner_claim_hash"]
                or row["stop_reservation_id"]
            ):
                return _refusal(
                    AbandonStatus.REFUSED_STATE,
                    "The retained-work record must be parked or failed and unowned",
                )

            attached = tuple(
                item["evidence_id"]
                for item in conn.execute(
                    "SELECT evidence_id FROM validated_work_evidence "
                    "WHERE record_id=? AND role='attached' "
                    "ORDER BY admitted_at,evidence_id",
                    (row["record_id"],),
                )
            )
            if attached:
                return AbandonValidatedWorkOutcome(
                    AbandonStatus.ATTACHED_EVIDENCE_PENDING,
                    None,
                    attached,
                    None,
                    "Newer retained evidence must be considered before abandonment",
                )

            resolved_at = self._timestamp()
            updated = conn.execute(
                "UPDATE validated_work_records SET state='abandoned',failure='',reason=?,"
                "resolution_kind=?,resolved_by=?,resolution_reason=?,resolved_at=?,"
                "terminal_at=?,updated_at=?,superseded_by_record_id='',waits_on_record_id='',"
                "abandon_authority_json=? WHERE record_id=? "
                "AND state IN ('parked','failed') AND owner_claim_hash='' "
                "AND stop_reservation_id='' AND EXISTS ("
                "SELECT 1 FROM validated_work_evidence e WHERE e.record_id=validated_work_records.record_id "
                "AND e.evidence_id=? AND e.role='current' AND e.observation_revision=?"
                ")",
                (
                    command.reason,
                    ResolutionKind.OPERATOR_ABANDONED.value,
                    command.actor,
                    command.reason,
                    resolved_at,
                    resolved_at,
                    resolved_at,
                    canonical_json(command.authority.to_dict()),
                    row["record_id"],
                    command.authority.evidence_id,
                    command.authority.observation_revision,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("validated-work abandonment CAS lost its transaction")
            self._lineage.classify(conn, row["lineage_key"], resolved_at)
            return AbandonValidatedWorkOutcome(
                AbandonStatus.ABANDONED,
                disposition(conn, row["record_id"]),
                (),
                None,
                "Retained validated work was abandoned by the operator",
            )

    def _timestamp(self) -> str:
        instant = self._clock()
        if type(instant) is not datetime or instant.tzinfo is None:
            raise ValueError("abandonment clock must return an aware datetime")
        value = instant.astimezone(UTC).isoformat()
        retention_instant(value)
        return value


def _refusal(status: AbandonStatus, message: str) -> AbandonValidatedWorkOutcome:
    return AbandonValidatedWorkOutcome(status, None, (), None, message)


def _stale(
    status: AbandonStatus,
    authority: ValidatedWorkAuthoritySnapshot,
    message: str,
) -> AbandonValidatedWorkOutcome:
    return AbandonValidatedWorkOutcome(status, None, (), authority, message)
