"""Read-only evidence lookup for doctor; never creates or initializes a database."""

import sqlite3
from contextlib import closing
from pathlib import Path

from ..domain.validated_work_store import EvidenceLookup
from .validated_work_rows import disposition, evidence_row


class SqliteValidatedWorkEvidenceReader:
    def __init__(self, path: Path) -> None:
        self._path = path

    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None:
        if not self._path.exists():
            return None
        # Explicit mode=ro allows live WAL coordination but no application writes.
        with closing(
            sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM validated_work_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            return (
                None
                if row is None
                else EvidenceLookup(
                    evidence_row(row), disposition(conn, row["record_id"])
                )
            )
