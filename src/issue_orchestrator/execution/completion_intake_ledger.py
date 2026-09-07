"""Private intake table implementation of SqliteIssueRunLedger.

The connection callback remains inside that adapter. No transaction or SQL handle
crosses a port. Files are committed first; self-describing envelopes repair the
rename/SQLite crash window while the same write transaction serializes closure.
"""

import json
import secrets
import os
from tempfile import mkstemp
from dataclasses import asdict
from ..domain.historical_intake import HistoricalIntakeCommand
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from ..domain.completion_intake import (
    CompletionIntakeEntry,
    CompletionIntakeError,
    CompletionParseStatus,
    CompletionValidationAttestation,
    IntakeClosed,
    IntakeUnauthorized,
    OwnedCompletionSubmission,
    OwnedValidationResult,
    SubmissionConflict,
    SubmissionOrigin,
    SubmitCompletionEvidence,
)
from .completion_intake_codec import SubmissionEnvelope, AttestationEnvelope, run_key
from ..domain.registered_completion import CompletionRunRole
from .issue_run_codec import IssueRunRow
from ..domain.completion_custody import validation_custody_blobs
from ..domain.issue_run_evidence import IssueRunRecord, IssueRunEvidenceUnavailable
from ..domain.models import CompletionRecord
from ..domain.completion_custody_integrity import (
    require_owned_result,
)
from ..domain.session_run import SessionRunAssets, SessionRunIdentity, RunContainedFile
from .completion_intake_artifacts import (
    CompletionIntakeArtifacts,
    canonical_bytes,
    read_regular,
    sync_directory,
)


class LedgerConnection(Protocol):
    def __call__(
        self, *, write: bool = False
    ) -> AbstractContextManager[sqlite3.Connection]: ...


class CompletionIntakeTables:
    def __init__(
        self,
        connect: LedgerConnection,
        decode: Callable[[sqlite3.Row], IssueRunRecord],
        root: Path,
    ) -> None:
        self._open = connect
        self._decode = decode
        self.artifacts = CompletionIntakeArtifacts(root)
        with self._connect(write=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS completion_intake_runs (
                run_key TEXT PRIMARY KEY, session_name TEXT NOT NULL, run_id TEXT NOT NULL,
                started_at TEXT NOT NULL, capability TEXT NOT NULL UNIQUE, closed INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(session_name,run_id,started_at) REFERENCES issue_runs(session_name,run_id,started_at)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS completion_intake_entries (
                entry_id TEXT PRIMARY KEY, run_key TEXT NOT NULL REFERENCES completion_intake_runs(run_key),
                submission_key TEXT NOT NULL, receive_sequence INTEGER NOT NULL UNIQUE,
                raw_sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, raw_locator TEXT NOT NULL,
                parse_status TEXT NOT NULL, normalization_version INTEGER,
                normalized_sha256 TEXT, normalized_locator TEXT, envelope_json TEXT NOT NULL,
                UNIQUE(run_key,submission_key)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS completion_validation_attestations (
                entry_id TEXT PRIMARY KEY REFERENCES completion_intake_entries(entry_id),
                run_key TEXT NOT NULL REFERENCES completion_intake_runs(run_key),
                raw_sha256 TEXT NOT NULL, normalized_sha256 TEXT NOT NULL, head_sha TEXT NOT NULL,
                validator_digest TEXT NOT NULL, result_sha256 TEXT NOT NULL, result_locator TEXT NOT NULL,
                outcome INTEGER NOT NULL, recorded_at TEXT NOT NULL, envelope_json TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS completion_intake_processing (
                entry_id TEXT PRIMARY KEY REFERENCES completion_intake_entries(entry_id),
                processed_at TEXT NOT NULL
            )""")
            for table in (
                "completion_intake_entries",
                "completion_validation_attestations",
            ):
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_immutable BEFORE UPDATE ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'immutable completion authority'); END"
                )
        self.repair()

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            with self._open(write=write) as conn:
                yield conn
        except (sqlite3.Error, IssueRunEvidenceUnavailable) as exc:
            raise CompletionIntakeError(
                "durable completion intake unavailable"
            ) from exc

    def allocate_run(self, conn: sqlite3.Connection, run: SessionRunAssets) -> None:
        """Private allocation transaction hook; the run row and capability commit together."""
        conn.execute(
            "INSERT INTO completion_intake_runs VALUES (?,?,?,?,?,0)",
            (
                run_key(run.identity),
                run.session_name,
                run.run_id,
                run.started_at,
                secrets.token_urlsafe(48),
            ),
        )

    def provision(self, run: SessionRunAssets) -> str:
        with self._connect() as conn:
            self._allocated(conn, run)
            row = conn.execute(
                "SELECT capability,closed FROM completion_intake_runs WHERE run_key=?",
                (run_key(run.identity),),
            ).fetchone()
            if row is None:
                raise IntakeUnauthorized(
                    "run has no allocation-bound intake capability"
                )
            if row["closed"]:
                raise IntakeClosed("completion intake is closed")
            return str(row["capability"])

    def capability_file(self, run: SessionRunAssets) -> RunContainedFile:
        """Launch commands contain only this locator, never a loggable secret."""
        capability = self.provision(run).encode("ascii")
        root = self.artifacts.root.parent / "completion-intake-capabilities"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.resolve() != root:
            raise CompletionIntakeError("capability storage is not canonical")
        path = root / run_key(run.identity)
        if path.exists():
            if read_regular(path) != capability:
                raise CompletionIntakeError("allocated capability file changed")
        else:
            fd, temporary = mkstemp(prefix=".staging-", dir=root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(capability)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path, follow_symlinks=False)
                except FileExistsError:
                    if read_regular(path) != capability:
                        raise CompletionIntakeError("allocated capability file changed")
                sync_directory(root)
                sync_directory(root.parent)
            finally:
                os.unlink(temporary)
        return RunContainedFile(root, path)

    def role(self, entry_id: str) -> CompletionRunRole:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT issue_runs.* FROM completion_intake_entries "
                "JOIN completion_intake_runs USING(run_key) "
                "JOIN issue_runs USING(session_name,run_id,started_at) WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise CompletionIntakeError("recorded completion agent role is missing")
            try:
                return IssueRunRow(dict(row)).processing_role()
            except (ValueError, TypeError) as exc:
                raise CompletionIntakeError("recorded completion role is invalid") from exc

    def run_for_capability(self, capability: str) -> SessionRunAssets:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT issue_runs.* FROM completion_intake_runs JOIN issue_runs "
                "USING(session_name,run_id,started_at) WHERE capability=? AND closed=0",
                (capability,),
            ).fetchone()
            if row is None:
                raise IntakeUnauthorized("invalid completion capability")
            return self._decode(row).run

    def submit(
        self, capability: str, command: SubmitCompletionEvidence
    ) -> CompletionIntakeEntry:
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT issue_runs.* FROM completion_intake_runs JOIN issue_runs "
                "USING(session_name,run_id,started_at) WHERE capability=?",
                (capability,),
            ).fetchone()
            if row is None:
                raise IntakeUnauthorized("invalid completion capability")
            run = self._decode(row).run
            return self._register(
                conn,
                run,
                OwnedCompletionSubmission(
                    command, SubmissionOrigin.RUN_CAPABILITY, "allocated_run"
                ),
            )

    def register(
        self, run: SessionRunAssets, submission: OwnedCompletionSubmission
    ) -> CompletionIntakeEntry:
        with self._connect(write=True) as conn:
            return self._register(conn, run, submission)

    def _allocated(self, conn: sqlite3.Connection, run: SessionRunAssets) -> None:
        row = conn.execute(
            "SELECT * FROM issue_runs WHERE session_name=? AND run_id=? AND started_at=?",
            (run.session_name, run.run_id, run.started_at),
        ).fetchone()
        if row is None or self._decode(row).run != run:
            raise IntakeUnauthorized("submission does not belong to allocated assets")
        if self.artifacts.root.is_relative_to(run.worktree_path):
            raise CompletionIntakeError("custody must be outside agent worktrees")

    def _register(
        self,
        conn: sqlite3.Connection,
        run: SessionRunAssets,
        submission: OwnedCompletionSubmission,
    ) -> CompletionIntakeEntry:
        self._repair(conn)
        self._allocated(conn, run)
        key = run_key(run.identity)
        command = submission.command
        entry_id = sha256(canonical_bytes([key, command.submission_key])).hexdigest()
        # Repair the exact orphan before retry matching (also after failed writes in-process).
        if (self.artifacts.root / entry_id).exists():
            self._insert_entry(conn, self.artifacts.read(entry_id))
        existing = conn.execute(
            "SELECT envelope_json FROM completion_intake_entries WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if existing is not None:
            entry = self._entry(conn, json.loads(existing[0]))
            if entry.raw_sha256 != command.content_sha256:
                raise SubmissionConflict("submission key already names different bytes")
            return entry
        owner = conn.execute(
            "SELECT closed FROM completion_intake_runs WHERE run_key=?", (key,)
        ).fetchone()
        if owner is None:
            raise IntakeUnauthorized("allocated run has no intake capability")
        if owner[0]:
            raise IntakeClosed("completion intake is closed")
        sequence = conn.execute(
            "SELECT COALESCE(MAX(receive_sequence),0)+1 FROM completion_intake_entries"
        ).fetchone()[0]
        blobs = {"raw.json": command.raw_bytes}
        parse_status = CompletionParseStatus.REJECTED
        normalized_hash = None
        try:
            payload = json.loads(command.raw_bytes)
            record = CompletionRecord.from_dict(payload)
            record.session_id = run.session_name
            record.validation_record_path = str(
                run.run_dir / "completion-intake" / entry_id / "validation.json"
            )
            normalized = canonical_bytes(record.to_dict())
            normalized_hash = sha256(normalized).hexdigest()
            blobs["completion.json"] = normalized
            parse_status = CompletionParseStatus.ACCEPTED
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            UnicodeError,
            RecursionError,
        ):
            pass  # Rejection is an immutable inspectable fact, never lost bytes.
        envelope: dict[str, object] = {
            "kind": "submission",
            "entry_id": entry_id,
            "run_key": key,
            "submission_key": command.submission_key,
            "receive_sequence": sequence,
            "raw_sha256": command.content_sha256,
            "byte_size": len(command.raw_bytes),
            "parse_status": parse_status.value,
            "normalization_version": 1 if normalized_hash else None,
            "normalized_sha256": normalized_hash,
            "origin": submission.origin.value,
            "actor": submission.actor,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        if submission.historical_command is not None:
            selection = asdict(submission.historical_command)
            selection["candidate_path"] = str(
                submission.historical_command.candidate_path
            )
            envelope["historical_command"] = selection
        self.artifacts.write(entry_id, envelope, blobs)
        envelope = self.artifacts.read(entry_id)
        self._insert_entry(conn, envelope)
        return self._entry(conn, envelope)

    def _insert_entry(
        self, conn: sqlite3.Connection, envelope: dict[str, object]
    ) -> None:
        codec = SubmissionEnvelope(envelope)
        existing = conn.execute(
            "SELECT envelope_json FROM completion_intake_entries WHERE entry_id=?",
            (codec.entry_id,),
        ).fetchone()
        if existing is not None:
            codec.require_existing(existing[0])
            return
        conn.execute(
            "INSERT INTO completion_intake_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            codec.insert_values(),
        )

    def _entry(
        self, conn: sqlite3.Connection, data: dict[str, object]
    ) -> CompletionIntakeEntry:
        codec = SubmissionEnvelope(data)
        row = conn.execute(
            "SELECT issue_runs.* FROM completion_intake_runs JOIN issue_runs "
            "USING(session_name,run_id,started_at) WHERE run_key=?",
            (codec.run_key,),
        ).fetchone()
        if row is None:
            raise CompletionIntakeError("custody run is missing")
        stored = conn.execute(
            "SELECT * FROM completion_intake_entries WHERE entry_id=?",
            (codec.entry_id,),
        ).fetchone()
        codec.verify(
            stored,
            self.artifacts.read(codec.entry_id),
            read_regular(self.artifacts.root / codec.entry_id / "raw.json"),
        )
        return codec.decode(self._decode(row).run, self.artifacts.root)

    def historical_command(self, entry_id: str) -> HistoricalIntakeCommand:
        with self._connect() as conn:
            entry = self._get(conn, entry_id)
            data = self.artifacts.read(entry_id)
            if entry.origin is not SubmissionOrigin.HISTORICAL_OPERATOR:
                raise CompletionIntakeError("receipt is not historical intake")
            command = data["historical_command"]
            if not isinstance(command, dict):
                raise CompletionIntakeError("historical selection is missing")
            return HistoricalIntakeCommand(
                command["repo_slug"],
                command["issue_number"],
                command["branch_name"],
                command["target_head_sha"],
                Path(command["candidate_path"]),
                command["candidate_sha256"],
                command["actor"],
                command["reason"],
            )

    def entry(self, entry_id: str) -> CompletionIntakeEntry:
        with self._connect() as conn:
            return self._get(conn, entry_id)

    def _get(self, conn: sqlite3.Connection, entry_id: str) -> CompletionIntakeEntry:
        row = conn.execute(
            "SELECT envelope_json FROM completion_intake_entries WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise CompletionIntakeError("unknown receipt")
        return self._entry(conn, json.loads(row[0]))

    def entries(self, run: SessionRunIdentity) -> tuple[CompletionIntakeEntry, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT envelope_json FROM completion_intake_entries WHERE run_key=? ORDER BY receive_sequence",
                (run_key(run),),
            ).fetchall()
            return tuple(self._entry(conn, json.loads(row[0])) for row in rows)

    def pending(self) -> tuple[CompletionIntakeEntry, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT envelope_json FROM completion_intake_entries WHERE entry_id NOT IN "
                "(SELECT entry_id FROM completion_intake_processing) ORDER BY receive_sequence"
            ).fetchall()
            return tuple(self._entry(conn, json.loads(row[0])) for row in rows)

    def processed(self, entry_id: str) -> None:
        with self._connect(write=True) as conn:
            self._get(conn, entry_id)
            conn.execute(
                "INSERT OR IGNORE INTO completion_intake_processing VALUES (?,?)",
                (entry_id, datetime.now(timezone.utc).isoformat()),
            )

    def close_run(self, run: SessionRunAssets) -> None:
        with self._connect(write=True) as conn:
            self._allocated(conn, run)
            self._repair(conn)
            conn.execute(
                "UPDATE completion_intake_runs SET closed=1 WHERE run_key=?",
                (run_key(run.identity),),
            )

    def issue_entries(self, issue_number: int) -> tuple[CompletionIntakeEntry, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT envelope_json FROM completion_intake_entries JOIN completion_intake_runs USING(run_key) "
                "JOIN issue_runs USING(session_name,run_id,started_at) WHERE issue_number=? ORDER BY receive_sequence",
                (issue_number,),
            ).fetchall()
            return tuple(self._entry(conn, json.loads(row[0])) for row in rows)

    def close_issue(self, issue_number: int) -> None:
        with self._connect(write=True) as conn:
            self._repair(conn)
            conn.execute(
                "UPDATE completion_intake_runs SET closed=1 WHERE run_key IN "
                "(SELECT run_key FROM completion_intake_runs JOIN issue_runs USING(session_name,run_id,started_at) WHERE issue_number=?)",
                (issue_number,),
            )

    def attest(
        self, entry_id: str, result: OwnedValidationResult
    ) -> CompletionIntakeEntry:
        with self._connect(write=True) as conn:
            entry = self._get(conn, entry_id)
            require_owned_result(entry, result)
            envelope: dict[str, object] = {
                "kind": "validation",
                "entry_id": entry_id,
                "run_key": run_key(entry.run.identity),
                "raw_sha256": entry.raw_sha256,
                "normalized_sha256": entry.normalized_sha256,
                "head_sha": result.head_sha,
                "validator_digest": result.validator_digest,
                "result_sha256": sha256(result.result_bytes).hexdigest(),
                "passed": result.passed,
                "recorded_at": result.recorded_at,
            }
            self.artifacts.write(
                entry_id + "-validation",
                envelope,
                validation_custody_blobs(result),
            )
            self._insert_attestation(
                conn, self.artifacts.read(entry_id + "-validation")
            )
            return entry

    def _insert_attestation(
        self, conn: sqlite3.Connection, data: dict[str, object]
    ) -> None:
        entry_id = str(data["entry_id"])
        entry = self._get(conn, entry_id)
        codec = AttestationEnvelope(data, entry)
        existing = conn.execute(
            "SELECT envelope_json FROM completion_validation_attestations WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if existing:
            codec.require_existing(existing[0])
            return
        result_bytes = read_regular(
            self.artifacts.root / (entry_id + "-validation") / "validation.json"
        )
        if sha256(result_bytes).hexdigest() != data["result_sha256"]:
            raise CompletionIntakeError("attestation result hash mismatch")
        # The run copy is a certified locator, never authority. The authoritative
        # bytes remain outside the worktree; consumers check the attested hash.
        if not entry.run.worktree_path.is_dir():
            raise CompletionIntakeError(
                "allocated run workspace unavailable for certification"
            )
        copy = CompletionIntakeArtifacts(entry.run.run_dir / "completion-intake")
        copy.write(
            entry_id,
            {"entry_id": entry_id, "result_sha256": data["result_sha256"]},
            {"validation.json": result_bytes},
        )
        conn.execute(
            "INSERT INTO completion_validation_attestations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            codec.insert_values(),
        )

    def attestation(self, entry_id: str) -> CompletionValidationAttestation | None:
        with self._connect() as conn:
            return self._attestation(conn, entry_id)

    def _attestation(
        self, conn: sqlite3.Connection, entry_id: str
    ) -> CompletionValidationAttestation | None:
        entry = self._get(conn, entry_id)
        row = conn.execute(
            "SELECT * FROM completion_validation_attestations WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        codec = AttestationEnvelope(json.loads(row["envelope_json"]), entry)
        codec.verify(row, self.artifacts.read(entry_id + "-validation"))
        return codec.decode(self.artifacts.root)

    def repair(self) -> None:
        with self._connect(write=True) as conn:
            self._repair(conn)

    def _repair(self, conn: sqlite3.Connection) -> None:
        envelopes = [self.artifacts.read(key) for key in self.artifacts.envelopes()]
        for data in sorted(
            (item for item in envelopes if item["kind"] == "submission"),
            key=lambda item: int(str(item["receive_sequence"])),
        ):
            self._insert_entry(conn, data)
        for data in envelopes:
            if data["kind"] == "validation":
                self._insert_attestation(conn, data)
        for row in conn.execute("SELECT envelope_json FROM completion_intake_entries"):
            self._entry(conn, json.loads(row[0]))
        for row in conn.execute(
            "SELECT entry_id FROM completion_validation_attestations"
        ):
            self._attestation(conn, row[0])
