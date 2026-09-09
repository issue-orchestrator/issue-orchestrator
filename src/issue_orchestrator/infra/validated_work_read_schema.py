"""Structural compatibility check for cold validated-work reads."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)
from .sqlite_connection import readonly_sqlite_transaction


_REQUIRED_COLUMNS = {
    "validated_work_records": frozenset(
        {
            "record_id",
            "repo_slug",
            "issue_number",
            "branch_name",
            "validated_head_sha",
            "lineage_role",
            "waits_on_record_id",
            "owner_fence",
            "owner_host",
            "owner_pid",
            "owner_started_at",
            "owner_claim_hash",
            "owner_instance_id",
            "stop_reservation_id",
            "state",
            "failure",
            "reason",
            "finalization_phase",
            "published_head_sha",
            "resolved_by",
            "resolution_reason",
            "resolved_at",
            "updated_at",
        }
    ),
    "validated_work_evidence": frozenset(
        {
            "evidence_id",
            "record_id",
            "role",
            "identity",
            "observations",
            "worktree_head_sha",
            "expected_remote_head",
            "pr_number",
            "escrow_dir",
            "pinned_ref",
            "observed_ref",
            "observation_revision",
            "initial_state",
            "initial_failure",
            "initial_reason",
            "base_state",
            "base_failure",
            "base_reason",
            "admitted_at",
            "role_changed_at",
            "released_at",
        }
    ),
    "validated_work_publish_attempts": frozenset({"record_id"}),
}


def require_supported_validated_work_schema(conn: sqlite3.Connection) -> None:
    """Accept additive schema changes and reject missing facts needed by the mapper."""
    missing: list[str] = []
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        absent = sorted(required - actual)
        if absent:
            missing.append(f"{table}: {', '.join(absent)}")
    if missing:
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.UNSUPPORTED_SCHEMA,
            "Validated-work schema is unsupported (" + "; ".join(missing) + ")",
        )


@contextmanager
def validated_work_read_transaction(
    path: Path, *, timeout: float
) -> Iterator[sqlite3.Connection]:
    """Open one supported-schema read snapshot through the shared SQLite profile."""
    with readonly_sqlite_transaction(
        path, timeout=timeout, row_factory=sqlite3.Row
    ) as connection:
        require_supported_validated_work_schema(connection)
        yield connection
