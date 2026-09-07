"""Durable exact run ownership retained outside disposable worktrees."""

from ..domain.validated_work import ValidatedWorkEvidence

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from .completion_intake_ledger import CompletionIntakeTables
from ..domain.prepared_completion import PreparedCompletionEvidence
from .issue_run_schema import create_issue_run_schema, validate_issue_run_schema
from ..domain.completion_intake import (
    CompletionIntakeEntry,
    CompletionValidationAttestation,
    OwnedCompletionSubmission,
    OwnedValidationResult,
    SubmitCompletionEvidence,
)
from ..domain.session_run import SessionRunIdentity, RunContainedFile
from ..domain.registered_completion import CompletionRunRole

from ..domain.historical_intake import HistoricalIntakeCommand
from ..domain.models import CompletionRecord
from .issue_run_codec import IssueRunRow
from ..domain.issue_run_evidence import IssueRunEvidenceUnavailable, IssueRunRecord
from ..domain.session_run import SessionRunAssets
from ..infra.sqlite_connection import open_sqlite


class SqliteIssueRunLedger:
    """Append-only launch facts; neither age nor launch failure erases ownership.

    Registration is idempotent only for the same issue, key and exact assets.
    A conflicting registration raises before any launch can proceed. Reads never
    initialize missing schema: losing a database during runtime is not no work.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        marker = db_path.with_suffix(db_path.suffix + ".initialized")
        if marker.exists():
            self._identity = marker.read_text(encoding="ascii")
            self._validate_existing()
            with self._connect(write=True) as conn:
                if "branch_name" not in {row[1] for row in conn.execute("PRAGMA table_info(issue_runs)")}:
                    conn.execute("ALTER TABLE issue_runs ADD COLUMN branch_name TEXT")
                if "terminal_binding" not in {row[1] for row in conn.execute("PRAGMA table_info(issue_runs)")}:
                    conn.execute("ALTER TABLE issue_runs ADD COLUMN terminal_binding TEXT")
            self._intake = CompletionIntakeTables(
                self._connect, self._decode, db_path.parent / "completion-intake"
            )
            return
        if db_path.exists():
            raise IssueRunEvidenceUnavailable("Run ledger initialization identity is missing")
        self._identity = uuid4().hex
        # Write intent before creating schema. A crash during initialization
        # leaves an explicit refusal, never permission to erase existing facts.
        with marker.open("x", encoding="ascii") as stream:
            stream.write(self._identity)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(db_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        with closing(open_sqlite(db_path)) as conn, conn:
            create_issue_run_schema(conn, self._identity)

        self._intake = CompletionIntakeTables(
            self._connect, self._decode, db_path.parent / "completion-intake"
        )

    def _validate_existing(self) -> None:
        try:
            with self._connect(write=True) as conn:
                validate_issue_run_schema(conn)
        except sqlite3.Error as exc:
            raise IssueRunEvidenceUnavailable("Established run ledger schema is unavailable") from exc

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Bind every transaction to the established durable ledger identity."""
        if not self._path.is_file():
            raise IssueRunEvidenceUnavailable(f"Run ledger disappeared: {self._path}")
        with closing(open_sqlite(self._path, row_factory=sqlite3.Row)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            identities = conn.execute("SELECT identity FROM issue_run_ledger_identity").fetchall()
            if [row[0] for row in identities] != [self._identity]:
                raise IssueRunEvidenceUnavailable("Run ledger initialization identity changed")
            yield conn

    def record_run(self, issue_number: int, record: IssueRunRecord) -> None:
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("run ownership requires a positive issue number")
        codec = IssueRunRow.from_record(issue_number, record)
        try:
            with self._connect(write=True) as conn:
                existing = conn.execute(codec.select_sql, codec.key_values).fetchone()
                if existing is not None:
                    codec.require_existing(existing)
                    return
                conn.execute(codec.insert_sql, codec.insert_values)
                self._intake.allocate_run(conn, record.run)
        except sqlite3.Error as exc:
            raise IssueRunEvidenceUnavailable(
                "Could not persist run ownership"
            ) from exc


    def issue_numbers(self) -> tuple[int, ...]:
        with self._connect() as conn:
            return tuple(row[0] for row in conn.execute("SELECT DISTINCT issue_number FROM issue_runs ORDER BY issue_number"))

    def recorded_runs(self, issue_number: int) -> tuple[IssueRunRecord, ...]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM issue_runs WHERE issue_number=? "
                    "ORDER BY recorded_at, session_name, run_id, started_at", (issue_number,),
                ).fetchall()
            return tuple(self._decode(row) for row in rows)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise IssueRunEvidenceUnavailable("Could not read run ownership") from exc

    @staticmethod
    def _decode(row: sqlite3.Row) -> IssueRunRecord:
        return IssueRunRow(dict(row)).decode()

    def role_for_receipt(self, entry_id: str) -> CompletionRunRole:
        return self._intake.role(entry_id)

    def recorded_run(self, run: SessionRunAssets) -> IssueRunRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM issue_runs WHERE session_name=? AND run_id=? AND started_at=?",
                (run.session_name, run.run_id, run.started_at)).fetchone()
            if row is None:
                raise IssueRunEvidenceUnavailable("exact allocated run is not registered")
            record = self._decode(row)
            if record.run != run:
                raise IssueRunEvidenceUnavailable("exact allocated run assets differ from ledger")
            return record

    def run_for_capability(self, capability: str) -> SessionRunAssets:
        return self._intake.run_for_capability(capability)

    def submission_capability(self, run: SessionRunAssets) -> str:
        return self._intake.provision(run)

    def submit(
        self, capability: str, command: SubmitCompletionEvidence
    ) -> CompletionIntakeEntry:
        return self._intake.submit(capability, command)

    def register_submission(
        self, run: SessionRunAssets, submission: OwnedCompletionSubmission
    ) -> CompletionIntakeEntry:
        return self._intake.register(run, submission)

    def attest_validation(
        self, entry_id: str, result: OwnedValidationResult
    ) -> CompletionIntakeEntry:
        return self._intake.attest(entry_id, result)

    def entries_for_run(
        self, run: SessionRunIdentity
    ) -> tuple[CompletionIntakeEntry, ...]:
        return self._intake.entries(run)

    def entry_for_receipt(self, entry_id: str) -> CompletionIntakeEntry:
        return self._intake.entry(entry_id)

    def validation_for_receipt(
        self, entry_id: str
    ) -> CompletionValidationAttestation | None:
        return self._intake.attestation(entry_id)

    def pending_receipts(self) -> tuple[CompletionIntakeEntry, ...]:
        return self._intake.pending()

    def mark_processed(self, entry_id: str) -> None:
        self._intake.processed(entry_id)

    def close_intake(self, issue_number: int) -> None:
        self._intake.close_issue(issue_number)

    def repair_intake(self) -> None:
        self._intake.repair()

    def read_owned_completion(self, entry_id: str) -> "CompletionRecord":
        from .completion_intake_artifacts import read_regular

        entry = self.entry_for_receipt(entry_id)
        if entry.normalized_path is None:
            raise ValueError("rejected receipt has no normalized completion")
        return CompletionRecord.from_dict(json.loads(read_regular(entry.normalized_path)))

    def evidence_receive_sequence(self, evidence: ValidatedWorkEvidence) -> int:
        from .completion_intake_candidates import evidence_receive_sequence
        return evidence_receive_sequence(self, evidence)

    def prepare_candidate(self, entry_id: str, run: IssueRunRecord) -> "PreparedCompletionEvidence | None":
        from .completion_intake_candidates import prepare_candidate

        return prepare_candidate(self, entry_id, run)

    def read_completion(self, entry_id: str) -> "CompletionRecord":
        from ..domain.models import CompletionRecord
        from .completion_intake_artifacts import read_regular

        entry = self.entry_for_receipt(entry_id)
        if entry.normalized_path is None:
            raise ValueError("rejected receipt has no normalized completion")
        record = CompletionRecord.from_dict(
            json.loads(read_regular(entry.normalized_path))
        )
        attestation = self.validation_for_receipt(entry_id)
        if attestation is not None:
            from hashlib import sha256
            from ..domain.completion_intake import CompletionIntakeError

            certified_path = (
                entry.run.run_dir / "completion-intake" / entry_id / "validation.json"
            )
            if (
                record.validation_record_path != str(certified_path)
                or sha256(read_regular(certified_path)).hexdigest()
                != attestation.result_sha256
            ):
                raise CompletionIntakeError(
                    "run validation copy does not match attestation"
                )
        return record

    def close_run_intake(self, run: SessionRunAssets) -> None:
        self._intake.close_run(run)

    def entries_for_issue(self, issue_number: int) -> tuple[CompletionIntakeEntry, ...]:
        return self._intake.issue_entries(issue_number)

    def historical_command_for_receipt(self, entry_id: str) -> HistoricalIntakeCommand:
        return self._intake.historical_command(entry_id)

    def submission_capability_file(self, run: SessionRunAssets) -> RunContainedFile:
        return self._intake.capability_file(run)
