"""Role-complete admission and explicit attached-evidence drain."""

from __future__ import annotations

import sqlite3

from ..domain.validated_work import canonical_json, canonical_lineage_key
from ..domain.validated_work_store import (
    AdmissionStatus,
    EvidenceAdmission,
    EvidenceRole,
)
from .validated_work_lineage import LineageClassifier
from .validated_work_rows import current_evidence, record_row, refresh_observations


class EvidenceAdmissionWriter:
    def __init__(self, lineage: LineageClassifier) -> None:
        self._lineage = lineage

    def admit(
        self, conn: sqlite3.Connection, admission: EvidenceAdmission
    ) -> AdmissionStatus:
        evidence = admission.evidence
        known = conn.execute(
            "SELECT * FROM validated_work_evidence WHERE evidence_id=?",
            (evidence.evidence_id,),
        ).fetchone()
        if known is not None:
            return self._replay(conn, admission, known)
        admission.require_capture_gate()
        row = conn.execute(
            "SELECT * FROM validated_work_records WHERE record_id=?",
            (evidence.record_id,),
        ).fetchone()
        if row is None:
            self._insert_record(conn, admission)
            self._insert_evidence(conn, admission, EvidenceRole.CURRENT)
            self._classify(conn, admission)
            return AdmissionStatus.ADMITTED
        if row["state"] == "publishing":
            self._insert_evidence(conn, admission, EvidenceRole.ATTACHED)
            return AdmissionStatus.ATTACHED
        if row["state"] == "recovered":
            self._insert_evidence(conn, admission, EvidenceRole.SUPERSEDED)
            return AdmissionStatus.ALREADY_RECOVERED
        self._demote_current(conn, evidence.record_id, admission.admitted_at)
        self._insert_evidence(conn, admission, EvidenceRole.CURRENT)
        self._clear_resolution(conn, evidence.record_id)
        self._classify(conn, admission)
        return (
            AdmissionStatus.REOPENED
            if row["state"] == "abandoned"
            else AdmissionStatus.SUPERSEDES
        )

    def _replay(
        self, conn: sqlite3.Connection, admission: EvidenceAdmission, known: sqlite3.Row
    ) -> AdmissionStatus:
        if known["record_id"] != admission.evidence.record_id:
            raise ValueError("evidence belongs to another record")
        role = EvidenceRole(known["role"])
        if role is EvidenceRole.ATTACHED:
            return AdmissionStatus.ATTACHED
        if role is EvidenceRole.SUPERSEDED:
            return AdmissionStatus.RETAINED
        row = record_row(conn, known["record_id"])
        if row["state"] in {"queued", "parked", "failed"}:
            refresh_observations(
                conn, known["evidence_id"], admission.evidence.observations
            )
            conn.execute(
                "UPDATE validated_work_records SET updated_at=? WHERE record_id=?",
                (admission.admitted_at, row["record_id"]),
            )
            self._lineage.classify(conn, row["lineage_key"], admission.admitted_at)
        return AdmissionStatus.CONVERGED

    def resolve_attached(
        self, conn: sqlite3.Connection, record_id: str, at: str
    ) -> None:
        row = record_row(conn, record_id)
        if row["state"] == "publishing":
            return
        waiting = conn.execute(
            "SELECT evidence_id FROM validated_work_evidence WHERE record_id=? AND role='attached' ORDER BY admitted_at, evidence_id",
            (record_id,),
        ).fetchall()
        if not waiting:
            return
        if row["state"] == "abandoned":
            raise ValueError("abandoned work cannot hold unresolved attached evidence")
        if row["state"] == "recovered":
            conn.execute(
                "UPDATE validated_work_evidence SET role='superseded', role_changed_at=? WHERE record_id=? AND role='attached'",
                (at, record_id),
            )
            return
        self._demote_current(conn, record_id, at)
        conn.execute(
            "UPDATE validated_work_evidence SET role='current', role_changed_at=? WHERE evidence_id=?",
            (at, waiting[0]["evidence_id"]),
        )
        self._clear_resolution(conn, record_id)
        self._lineage.classify(
            conn, row["lineage_key"], at, reconsider=frozenset({record_id})
        )

    def _classify(self, conn: sqlite3.Connection, admission: EvidenceAdmission) -> None:
        evidence = admission.evidence
        self._lineage.classify(
            conn,
            canonical_lineage_key(evidence.identity.key),
            admission.admitted_at,
            reconsider=frozenset({evidence.record_id}),
        )

    @staticmethod
    def _insert_record(conn: sqlite3.Connection, admission: EvidenceAdmission) -> None:
        ev, at = admission.evidence, admission.admitted_at
        key = ev.identity.key
        # Park provisionally so a prior HEAD can be demoted before the index
        # admits this head. Classification installs the real gate before commit.
        conn.execute(
            "INSERT INTO validated_work_records (record_id, repo_slug, issue_number, branch_name, validated_head_sha, "
            "lineage_key, lineage_role, state, created_at, updated_at) VALUES (?,?,?,?,?,?,'head','parked',?,?)",
            (
                ev.record_id,
                key.repo_slug,
                key.issue_number,
                key.branch_name,
                key.validated_head_sha,
                canonical_lineage_key(key),
                at,
                at,
            ),
        )

    @staticmethod
    def _insert_evidence(
        conn: sqlite3.Connection, admission: EvidenceAdmission, role: EvidenceRole
    ) -> None:
        ev, obs = admission.evidence, admission.evidence.observations
        conn.execute(
            "INSERT INTO validated_work_evidence (evidence_id,record_id,role,identity,observations,worktree_head_sha,"
            "expected_remote_head,pr_number,escrow_dir,pinned_ref,observed_ref,initial_state,initial_failure,initial_reason,admitted_at,role_changed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ev.evidence_id,
                ev.record_id,
                role.value,
                canonical_json(ev.identity),
                canonical_json(obs),
                obs.worktree_head_sha,
                obs.expected_remote_head_sha or "",
                obs.pr_number,
                admission.escrow_dir,
                admission.pinned_ref,
                admission.observed_ref,
                admission.initial_state.value,
                admission.initial_failure.value if admission.initial_failure else "",
                admission.initial_reason,
                admission.admitted_at,
                admission.admitted_at,
            ),
        )

    @staticmethod
    def _demote_current(conn: sqlite3.Connection, record_id: str, at: str) -> None:
        current_evidence(conn, record_id)  # fail closed if the relation is damaged
        conn.execute(
            "UPDATE validated_work_evidence SET role='superseded',role_changed_at=? WHERE record_id=? AND role='current'",
            (at, record_id),
        )

    @staticmethod
    def _clear_resolution(conn: sqlite3.Connection, record_id: str) -> None:
        conn.execute(
            "UPDATE validated_work_records SET state='parked', failure='', reason='', finalization_phase='not_started', publishing_started_at='', "
            "published_head_sha='',resolution_kind='',resolved_by='',resolution_reason='',resolved_at='',terminal_at='' WHERE record_id=?",
            (record_id,),
        )
