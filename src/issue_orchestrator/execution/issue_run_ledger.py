"""Durable exact run ownership retained outside disposable worktrees."""

from ..domain.validated_work import ValidatedWorkEvidence

import json
import logging
import os
import sqlite3
from collections.abc import Iterator, Sequence
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

logger = logging.getLogger(__name__)


class SqliteIssueRunLedger:
    """Append-only launch facts; neither age nor launch failure erases ownership.

    Registration is idempotent only for the same issue, key and exact assets.
    A conflicting registration raises before any launch can proceed. Reads never
    initialize missing schema: losing a database during runtime is not no work.
    """

    def __init__(self, db_path: Path, *, repo_slug: str) -> None:
        self._path = db_path
        self._repo_slug = repo_slug
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
                self._backfill_issue_scope(conn)
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

    def _backfill_issue_scope(self, conn: sqlite3.Connection) -> None:
        """Repair rows written before the scope was required.

        The orchestrator only ever records runs for its own repository, so an
        unscoped row is unambiguously this one's. Repairing beats refusing:
        those rows describe real sessions, and leaving them unreadable would
        strand every consumer that sweeps the whole ledger (#7255).
        """
        if not self._repo_slug.strip():
            return
        # ONE definition of "blank", shared with `_is_readable`. Spelling a
        # whitespace set into SQL drifted from Python's `str.strip()` twice
        # (spaces only, then ASCII only); selecting candidates and applying the
        # Python predicate makes the two agree by construction.
        candidates = [
            row[0] for row in conn.execute(
                "SELECT rowid, issue_scope FROM issue_runs WHERE issue_scope <> ?",
                (self._repo_slug.strip(),),
            ) if not str(row[1]).strip()
        ]
        if candidates:
            conn.executemany(
                "UPDATE issue_runs SET issue_scope=? WHERE rowid=?",
                # Stripped, exactly as `IssueRunRow.from_record` stores it, so a
                # padded slug cannot leave two spellings for one repository.
                [(self._repo_slug.strip(), rowid) for rowid in candidates],
            )
        repaired = len(candidates)
        if repaired:
            logger.warning(
                "[RUN_LEDGER] repaired %d run row(s) recorded with no repository "
                "scope; backfilled issue_scope=%s", repaired, self._repo_slug,
            )

    def recorded_runs(self, issue_number: int) -> tuple[IssueRunRecord, ...]:
        rows = self._rows_for_issue(issue_number)
        # Self-heal rather than refuse. A row inserted by another instance on an
        # older build lands here AFTER the open-time backfill, and raising would
        # fail `recorded_runs` for this issue -> `evidence_for_issue` ->
        # `issues_for_worktree`, which sweeps EVERY issue: worktree custody
        # cleanup stops repo-wide and re-plans every tick. That is the shape of
        # #7255 reintroduced by its own fix. Repairing keeps the sweep alive and
        # is still fail-closed, because nothing proceeds on unreadable evidence.
        if self._repair_needed(rows):
            self._repair_unscoped_rows()
            rows = self._rows_for_issue(issue_number)
        return tuple(self._decode_readable(row) for row in rows if self._is_readable(row))

    def _repair_needed(self, rows: "Sequence[sqlite3.Row]") -> bool:
        return bool(self._repo_slug.strip()) and any(
            not str(row["issue_scope"]).strip() for row in rows
        )

    def _repair_unscoped_rows(self) -> None:
        """Run the open-time backfill again, in this boundary's own error type.

        Every other failure in a read surfaces as `IssueRunEvidenceUnavailable`;
        a raw `sqlite3.OperationalError` from here would escape through
        `issues_for_worktree`, which does not wrap it.
        """
        try:
            with self._connect(write=True) as conn:
                self._backfill_issue_scope(conn)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise IssueRunEvidenceUnavailable("Could not repair run ownership") from exc

    def _rows_for_issue(self, issue_number: int) -> list[sqlite3.Row]:
        try:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT * FROM issue_runs WHERE issue_number=? "
                    "ORDER BY recorded_at, session_name, run_id, started_at", (issue_number,),
                ).fetchall()
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise IssueRunEvidenceUnavailable("Could not read run ownership") from exc

    def _is_readable(self, row: sqlite3.Row) -> bool:
        """Whether this row still carries a usable identity.

        Two different situations reach a blank row here, and they need different
        answers:

        * No slug to repair with (tests only -- `require_repo` at the
          composition root makes it impossible in production). Skip LOUDLY: one
          bad row failing the whole read stops worktree custody cleanup
          repo-wide, which is #7255's own shape.
        * A slug IS configured and the row is still blank after a repair pass --
          a writer raced us. Refuse. Returning an empty tuple would make
          `evidence_for_issue` report NO_RUNS_RECORDED, which capture reads as
          "nothing to preserve": a false all-clear over work it could not read.
          Refusing does fail the whole `issues_for_worktree` sweep for this tick
          -- it has no per-issue guard -- but the next read repairs the row, so
          this is transient and self-healing, unlike the permanent outage a
          non-repairing refusal caused.
        """
        if str(row["issue_scope"]).strip():
            return True
        if self._repo_slug.strip():
            raise IssueRunEvidenceUnavailable(
                f"run {row['run_id']} for issue #{row['issue_number']} is still "
                "unscoped after a repair pass; another writer may be racing"
            )
        logger.error(
            "[RUN_LEDGER] skipping run %s for issue #%s: recorded with no "
            "repository scope and no slug configured to repair it",
            row["run_id"], row["issue_number"],
        )
        return False

    def _decode_readable(self, row: sqlite3.Row) -> IssueRunRecord:
        try:
            return self._decode(row)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise IssueRunEvidenceUnavailable("Could not read run ownership") from exc

    @staticmethod
    def _decode(row: sqlite3.Row) -> IssueRunRecord:
        return IssueRunRow(dict(row)).decode()

    def role_for_receipt(self, entry_id: str) -> CompletionRunRole:
        return self._intake.role(entry_id)

    def recorded_run(self, run: SessionRunAssets) -> IssueRunRecord:
        # Heals like `recorded_runs`: this is the COMPLETION path
        # (`CompletionIntake._prepare_receipt`), so refusing here strands a
        # finished session -- the exact shape #7255 is about.
        row = self._exact_row(run)
        if row is not None and self._repair_needed([row]):
            self._repair_unscoped_rows()
            row = self._exact_row(run)
        if row is None:
            raise IssueRunEvidenceUnavailable("exact allocated run is not registered")
        record = self._decode_readable(row)
        if record.run != run:
            raise IssueRunEvidenceUnavailable("exact allocated run assets differ from ledger")
        return record

    def _exact_row(self, run: SessionRunAssets) -> "sqlite3.Row | None":
        with self._connect() as conn:
            return conn.execute("SELECT * FROM issue_runs WHERE session_name=? AND run_id=? AND started_at=?",
                (run.session_name, run.run_id, run.started_at)).fetchone()

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

    def prepare_evidence(self, evidence: ValidatedWorkEvidence) -> "PreparedCompletionEvidence":
        from .completion_intake_candidates import prepare_evidence
        return prepare_evidence(self, evidence)

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
