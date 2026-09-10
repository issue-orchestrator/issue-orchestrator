"""Cold, read-only SQLite adapter for retained validated-work facts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)
from ..domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from ..domain.validated_work import UNRESOLVED_STATES, require_text
from ..domain.validated_work_discovery import (
    ValidatedWorkDiscovery,
    ValidatedWorkSnapshot,
    WorkDiscoveryStatus,
)
from .repo_identity import state_dir
from .validated_work_read_schema import validated_work_read_transaction
from .validated_work_snapshot_mapper import ValidatedWorkSnapshotMapper


StopAvailability = Callable[[EngineIdentity], EngineStopAvailability]


class SqliteValidatedWorkRecordReader:
    """Read retained records without engine HTTP, claims, migrations or writes."""

    def __init__(
        self,
        *,
        timeout: float,
        stop_availability: StopAvailability,
    ) -> None:
        if stop_availability is None:
            raise ValueError("cold discovery requires a stop-availability owner")
        self._timeout = timeout
        self._snapshots = ValidatedWorkSnapshotMapper(stop_availability)

    def discover_repository(self, repo_root: str) -> ValidatedWorkDiscovery:
        root = self._repo_root(repo_root)
        try:
            with self._transaction(root) as conn:
                rows = conn.execute(
                    "SELECT record_id FROM validated_work_records "
                    "WHERE state IN (?,?,?,?) OR owner_claim_hash!='' "
                    "ORDER BY issue_number,branch_name,record_id",
                    tuple(state.value for state in sorted(UNRESOLVED_STATES)),
                ).fetchall()
                records = tuple(
                    self._snapshots.snapshot(conn, root, row["record_id"])
                    for row in rows
                )
        except ReadOnlySqliteAccessError as error:
            return self._unavailable(error)
        return ValidatedWorkDiscovery(
            WorkDiscoveryStatus.AVAILABLE,
            records,
            "Retained validated-work discovery completed",
        )

    def snapshot_record(
        self, repo_root: str, record_id: str
    ) -> ValidatedWorkSnapshot | None:
        require_text(record_id, "record id")
        root = self._repo_root(repo_root)
        with self._transaction(root) as conn:
            found = conn.execute(
                "SELECT 1 FROM validated_work_records WHERE record_id=?",
                (record_id,),
            ).fetchone()
            return (
                None
                if found is None
                else self._snapshots.snapshot(conn, root, record_id)
            )

    def _transaction(self, repo_root: Path):
        database = state_dir(repo_root) / "validated_work.sqlite"
        return validated_work_read_transaction(database, timeout=self._timeout)

    @staticmethod
    def _repo_root(repo_root: str) -> Path:
        require_text(repo_root, "repository root")
        return Path(repo_root).resolve()

    @staticmethod
    def _unavailable(error: ReadOnlySqliteAccessError) -> ValidatedWorkDiscovery:
        status = {
            ReadOnlySqliteFailure.DATABASE_ABSENT: WorkDiscoveryStatus.DATABASE_ABSENT,
            ReadOnlySqliteFailure.UNSUPPORTED_SCHEMA: WorkDiscoveryStatus.UNSUPPORTED_SCHEMA,
            ReadOnlySqliteFailure.UNREADABLE: WorkDiscoveryStatus.UNREADABLE,
            ReadOnlySqliteFailure.TIMEOUT: WorkDiscoveryStatus.UNREADABLE,
        }[error.reason]
        return ValidatedWorkDiscovery(status, (), str(error))
