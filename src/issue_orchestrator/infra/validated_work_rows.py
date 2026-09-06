"""SQLite transaction lifetime and lossless typed row mapping."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from ..domain.validated_work import (
    LineageRole,
    ValidatedWorkFailure,
    ValidatedWorkKey,
    ValidatedWorkObservations,
    ValidatedWorkState,
    canonical_json,
)
from ..domain.validated_work_commands import (
    OperatorResolution,
    ValidatedWorkDisposition,
)
from ..domain.validated_work_store import (
    DispositionPhase,
    EvidenceAdmission,
    EvidenceRole,
    EvidenceRow,
    LineagePublication,
    PublicationProvenance,
    PublishAttempt,
    PublishValidatedHeadStatus,
)
from .sqlite_connection import open_sqlite
from .validated_work_codec import decode_evidence
from .validated_work_schema import SCHEMA


class DispositionDatabase:
    """One connection per operation; missing established databases fail closed."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(open_sqlite(path)) as conn:
            conn.executescript(SCHEMA)
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError(
                    "validated-work database integrity check failed"
                )

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self._path.is_file():
            raise FileNotFoundError("validated-work database disappeared")
        with closing(open_sqlite(self._path, row_factory=sqlite3.Row)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn


def record_row(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM validated_work_records WHERE record_id=?", (record_id,)
    ).fetchone()
    if row is None:
        raise KeyError(record_id)
    return row


def evidence_row(row: sqlite3.Row) -> EvidenceRow:
    evidence = decode_evidence(row["identity"], row["observations"])
    if (
        evidence.record_id != row["record_id"]
        or evidence.evidence_id != row["evidence_id"]
    ):
        raise ValueError("stored evidence identity does not match its keys")
    obs = evidence.observations
    if (row["worktree_head_sha"], row["expected_remote_head"], row["pr_number"]) != (
        obs.worktree_head_sha,
        obs.expected_remote_head_sha or "",
        obs.pr_number,
    ):
        raise ValueError("stored observation columns disagree with evidence")
    admission = EvidenceAdmission(
        evidence,
        ValidatedWorkState(row["initial_state"]),
        ValidatedWorkFailure(row["initial_failure"])
        if row["initial_failure"]
        else None,
        row["initial_reason"],
        row["escrow_dir"],
        row["pinned_ref"],
        row["observed_ref"],
        row["admitted_at"],
    )
    return EvidenceRow(
        admission,
        EvidenceRole(row["role"]),
        row["observation_revision"],
        row["role_changed_at"],
        row["released_at"],
    )


def current_evidence(conn: sqlite3.Connection, record_id: str) -> EvidenceRow:
    row = conn.execute(
        "SELECT * FROM validated_work_evidence WHERE record_id=? AND role='current'",
        (record_id,),
    ).fetchone()
    if row is None:
        raise ValueError("record has no current evidence")
    return evidence_row(row)


def disposition(conn: sqlite3.Connection, record_id: str) -> ValidatedWorkDisposition:
    row = record_row(conn, record_id)
    evidence = current_evidence(conn, record_id)
    key = ValidatedWorkKey(
        row["repo_slug"],
        row["issue_number"],
        row["branch_name"],
        row["validated_head_sha"],
    )
    if key != evidence.admission.evidence.identity.key:
        raise ValueError("record columns disagree with current evidence identity")
    resolution = None
    if row["state"] == "abandoned":
        resolution = OperatorResolution(
            row["resolved_by"], row["resolution_reason"], row["resolved_at"]
        )
    return ValidatedWorkDisposition(
        record_id,
        evidence.admission.evidence.identity.key,
        evidence.evidence_id,
        ValidatedWorkState(row["state"]),
        LineageRole(row["lineage_role"]),
        row["reason"],
        ValidatedWorkFailure(row["failure"]) if row["failure"] else None,
        evidence.admission.evidence.observations.pr_number,
        row["published_head_sha"] or None,
        resolution,
    )


def publication(
    conn: sqlite3.Connection, lineage_key: str
) -> LineagePublication | None:
    row = conn.execute(
        "SELECT * FROM validated_work_lineage WHERE lineage_key=?", (lineage_key,)
    ).fetchone()
    if row is None:
        return None
    return LineagePublication(
        row["lineage_key"],
        row["published_head_sha"],
        row["published_by_record_id"],
        PublicationProvenance(row["published_via"]),
        row["published_pre_push_expected"],
        row["published_at"],
    )


def attempt_row(row: sqlite3.Row) -> PublishAttempt:
    return PublishAttempt(
        row["record_id"],
        row["attempt_no"],
        row["evidence_id"],
        row["target_head_sha"],
        row["expected_remote_head"],
        DispositionPhase(row["phase"]),
        row["fence"],
        row["started_at"],
        PublishValidatedHeadStatus(row["outcome"]) if row["outcome"] else None,
        ValidatedWorkFailure(row["failure"]) if row["failure"] else None,
        row["finished_at"],
    )


def refresh_observations(
    conn: sqlite3.Connection, evidence_id: str, observations: ValidatedWorkObservations
) -> None:
    conn.execute(
        "UPDATE validated_work_evidence SET observations=?, worktree_head_sha=?, expected_remote_head=?, "
        "pr_number=?, observation_revision=observation_revision+1 WHERE evidence_id=?",
        (
            canonical_json(observations),
            observations.worktree_head_sha,
            observations.expected_remote_head_sha or "",
            observations.pr_number,
            evidence_id,
        ),
    )
