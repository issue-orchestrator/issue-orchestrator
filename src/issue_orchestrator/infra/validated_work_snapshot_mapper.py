"""Map one validated-work SQLite snapshot into detached domain facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from ..domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)
from ..domain.validated_work import (
    EvidenceRole,
    FinalizationPhase,
    LineageRole,
    RemoteBaselineStatus,
    UNRESOLVED_STATES,
    ValidatedWorkState,
    require_positive,
)
from ..domain.validated_work_claim import ProcessIdentity
from ..domain.validated_work_discovery import (
    ClaimOwnerFact,
    ValidatedWorkSnapshot,
)
from ..domain.validated_work_commands import AbandonStatus, ValidatedWorkDisposition
from ..domain.validated_work_store import EvidenceRow
from .validated_work_claims import owner_identity
from .validated_work_rows import current_evidence, disposition, record_row


StopAvailability = Callable[[EngineIdentity], EngineStopAvailability]


@dataclass(frozen=True, slots=True)
class _DurableSnapshotFacts:
    row: sqlite3.Row
    disposition: ValidatedWorkDisposition
    evidence: EvidenceRow
    relations: tuple[sqlite3.Row, ...]
    superseded: tuple[str, ...]
    attached: tuple[str, ...]
    process: ProcessIdentity | None
    finalization_phase: FinalizationPhase
    attempts: int


class ValidatedWorkSnapshotMapper:
    """Own durable-row consistency and UI action availability projection."""

    def __init__(self, stop_availability: StopAvailability) -> None:
        if stop_availability is None:
            raise ValueError("snapshot mapping requires a stop-availability owner")
        self._stop_availability = stop_availability

    def snapshot(
        self, conn: sqlite3.Connection, repo_root: Path, record_id: str
    ) -> ValidatedWorkSnapshot:
        facts = self._durable_facts(conn, record_id)
        owner = self._owner_fact(repo_root, facts.row, facts.process)
        try:
            return self._assemble(record_id, facts, owner)
        except (KeyError, TypeError, ValueError) as error:
            raise _malformed_record(record_id, error) from error

    def _durable_facts(
        self, conn: sqlite3.Connection, record_id: str
    ) -> _DurableSnapshotFacts:
        try:
            row = record_row(conn, record_id)
            result = disposition(conn, record_id)
            evidence = current_evidence(conn, record_id)
            relations = tuple(
                conn.execute(
                    "SELECT evidence_id,role,released_at FROM validated_work_evidence "
                    "WHERE record_id=? ORDER BY admitted_at,evidence_id",
                    (record_id,),
                ).fetchall()
            )
            superseded, attached = self._evidence_groups(relations)
            if type(evidence.released_at) is not str:
                raise ValueError("current evidence release time must be text")
            process = owner_identity(row)
            if process is not None:
                require_positive(row["owner_fence"], "owner fence")
            finalization_phase = FinalizationPhase(row["finalization_phase"])
            attempts = conn.execute(
                "SELECT COUNT(*) FROM validated_work_publish_attempts WHERE record_id=?",
                (record_id,),
            ).fetchone()[0]
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise _malformed_record(record_id, error) from error
        return _DurableSnapshotFacts(
            row,
            result,
            evidence,
            relations,
            superseded,
            attached,
            process,
            finalization_phase,
            attempts,
        )

    @staticmethod
    def _assemble(
        record_id: str,
        facts: _DurableSnapshotFacts,
        owner: ClaimOwnerFact | None,
    ) -> ValidatedWorkSnapshot:
        result = facts.disposition
        evidence = facts.evidence
        stop_reserved = bool(facts.row["stop_reservation_id"])
        can_abandon = _can_abandon(
            result.state,
            owner,
            stop_reserved=stop_reserved,
            attached=facts.attached,
        )
        return ValidatedWorkSnapshot(
            disposition=result,
            record_id=record_id,
            validated_head_sha=result.key.validated_head_sha,
            worktree_head_sha=evidence.admission.evidence.observations.worktree_head_sha,
            branch_name=result.key.branch_name,
            expected_remote_head_sha=evidence.authority.expected_remote_head_sha,
            remote_baseline_status=evidence.authority.remote_baseline_status,
            superseded_evidence_ids=facts.superseded,
            attached_evidence_ids=facts.attached,
            lineage_role=result.lineage_role,
            escrow_retained=any(not item["released_at"] for item in facts.relations),
            observation_revision=evidence.observation_revision,
            waits_on_record_id=facts.row["waits_on_record_id"],
            owner=owner,
            publish_attempts=facts.attempts,
            finalization_phase=facts.finalization_phase,
            updated_at=facts.row["updated_at"],
            can_recover=_can_recover(
                state=result.state,
                lineage_role=result.lineage_role,
                owner=owner,
                stop_reserved=stop_reserved,
                current_retained=not evidence.released_at,
                remote_baseline=evidence.authority.remote_baseline_status,
            ),
            can_abandon=can_abandon,
            abandon_unavailable=_abandon_unavailable(
                state=result.state,
                owner=owner,
                stop_reserved=stop_reserved,
                attached=facts.attached,
                can_abandon=can_abandon,
            ),
        )

    @staticmethod
    def _evidence_groups(
        relations: tuple[sqlite3.Row, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        grouped: dict[EvidenceRole, list[str]] = {
            EvidenceRole.SUPERSEDED: [],
            EvidenceRole.ATTACHED: [],
        }
        for item in relations:
            role = EvidenceRole(item["role"])
            if type(item["released_at"]) is not str:
                raise ValueError("evidence release time must be text")
            if role in grouped:
                grouped[role].append(item["evidence_id"])
        return tuple(grouped[EvidenceRole.SUPERSEDED]), tuple(
            grouped[EvidenceRole.ATTACHED]
        )

    def _owner_fact(
        self,
        repo_root: Path,
        row: sqlite3.Row,
        process: ProcessIdentity | None,
    ) -> ClaimOwnerFact | None:
        if process is None:
            return None
        try:
            engine = EngineIdentity(
                repo_root=str(repo_root),
                instance_id=process.instance_id,
                host=process.host,
                label=process.instance_id or "default",
                process=process,
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise _malformed_record(row["record_id"], error) from error
        # The lifecycle probe is synchronous, so SQLite cannot preempt Python work.
        # The owning read transaction rejects an expired result after this returns.
        availability = self._stop_availability(engine)
        if type(availability) is not EngineStopAvailability:
            raise TypeError("stop-availability owner returned an untyped result")
        return ClaimOwnerFact(engine, row["owner_fence"], availability)


def _malformed_record(record_id: str, error: Exception) -> ReadOnlySqliteAccessError:
    return ReadOnlySqliteAccessError(
        ReadOnlySqliteFailure.UNREADABLE,
        f"Validated-work record {record_id!r} is malformed: {error}",
    )


def _can_recover(
    *,
    state: ValidatedWorkState,
    lineage_role: LineageRole,
    owner: ClaimOwnerFact | None,
    stop_reserved: bool,
    current_retained: bool,
    remote_baseline: RemoteBaselineStatus,
) -> bool:
    return (
        state is ValidatedWorkState.PARKED
        and lineage_role is LineageRole.HEAD
        and owner is None
        and not stop_reserved
        and current_retained
        and remote_baseline is RemoteBaselineStatus.OBSERVED
    )


def _can_abandon(
    state: ValidatedWorkState,
    owner: ClaimOwnerFact | None,
    *,
    stop_reserved: bool,
    attached: tuple[str, ...],
) -> bool:
    return (
        state in {ValidatedWorkState.PARKED, ValidatedWorkState.FAILED}
        and owner is None
        and not stop_reserved
        and not attached
    )


def _abandon_unavailable(
    *,
    state: ValidatedWorkState,
    owner: ClaimOwnerFact | None,
    stop_reserved: bool,
    attached: tuple[str, ...],
    can_abandon: bool,
) -> AbandonStatus | None:
    if can_abandon:
        return None
    if state not in UNRESOLVED_STATES:
        return AbandonStatus.ALREADY_RESOLVED
    if (
        state not in {ValidatedWorkState.PARKED, ValidatedWorkState.FAILED}
        or owner is not None
        or stop_reserved
    ):
        return AbandonStatus.REFUSED_STATE
    if attached:
        return AbandonStatus.ATTACHED_EVIDENCE_PENDING
    return AbandonStatus.REFUSED_STATE
