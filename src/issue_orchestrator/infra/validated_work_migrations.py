"""Fail-closed migrations for durable validated-work authority."""

from __future__ import annotations

import sqlite3

from ..domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkState,
    canonical_json,
)
from .validated_work_codec import load_document
from .validated_work_legacy import (
    LEGACY_REMOTE_AUTHORITY_REASON,
    normalize_legacy_remote_authority,
)


def migrate_evidence_base_gate(conn: sqlite3.Connection) -> None:
    """Add the mutable recovery gate while preserving admission audit facts."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(validated_work_evidence)")
    }
    for name in ("base_state", "base_failure", "base_reason"):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE validated_work_evidence ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )
    conn.execute(
        "UPDATE validated_work_evidence SET "
        "base_state=initial_state,base_failure=initial_failure,base_reason=initial_reason "
        "WHERE base_state=''"
    )


def migrate_remote_baseline_authority(conn: sqlite3.Connection) -> None:
    """Remove authority that predates the observed/unobserved provenance bit.

    Legacy null and SHA values cannot prove whether GitHub was read. Evidence is
    rewritten as unobserved, and a currently queued record is parked. Publishing
    records retain their in-flight lifecycle so an upgrade never erases an
    ambiguous remote side effect; later recovery must refresh authority.
    """
    with conn:
        # The write reservation must precede inspection. Otherwise a second
        # opener can refresh and claim evidence between this SELECT and the
        # first UPDATE, after which this migration would overwrite live facts.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT evidence_id,record_id,role,observations,initial_state,"
            "initial_failure,initial_reason "
            "FROM validated_work_evidence"
        ).fetchall()
        for row in rows:
            observations = load_document(row["observations"])
            if "remote_baseline_status" in observations:
                continue
            observations.update(
                remote_baseline_status=RemoteBaselineStatus.UNOBSERVED.value,
                expected_remote_head_sha=None,
                pr_number=None,
            )
            state, failure, reason = normalize_legacy_remote_authority(
                ValidatedWorkState(row["initial_state"]),
                ValidatedWorkFailure(row["initial_failure"])
                if row["initial_failure"]
                else None,
                row["initial_reason"],
            )
            conn.execute(
                "UPDATE validated_work_evidence SET observations=?,expected_remote_head='',"
                "pr_number=NULL,observation_revision=observation_revision+1,"
                "initial_state=?,initial_failure=?,initial_reason=?,"
                "base_state=?,base_failure=?,base_reason=? "
                "WHERE evidence_id=?",
                (
                    canonical_json(observations),
                    state.value,
                    failure.value if failure is not None else "",
                    reason,
                    state.value,
                    failure.value if failure is not None else "",
                    reason,
                    row["evidence_id"],
                ),
            )
            if row["role"] == "current":
                conn.execute(
                    "UPDATE validated_work_records SET state='parked',failure=?,reason=? "
                    "WHERE record_id=? AND state='queued'",
                    (
                        ValidatedWorkFailure.REMOTE_UNREADABLE.value,
                        LEGACY_REMOTE_AUTHORITY_REASON,
                        row["record_id"],
                    ),
                )
