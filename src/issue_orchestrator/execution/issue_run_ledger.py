"""Durable exact run ownership retained outside disposable worktrees."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from ..domain.issue_key import GitHubIssueKey
from ..domain.issue_run_evidence import IssueRunEvidenceUnavailable, IssueRunRecord
from ..domain.session_key import SessionKey, TaskKind
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
            conn.execute("CREATE TABLE issue_run_ledger_identity (identity TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO issue_run_ledger_identity VALUES (?)", (self._identity,))
            conn.execute("""
                CREATE TABLE issue_runs (
                    session_name TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    issue_number INTEGER NOT NULL CHECK(issue_number > 0),
                    issue_scope TEXT NOT NULL,
                    issue_key TEXT NOT NULL,
                    task TEXT NOT NULL,
                    assets_json TEXT NOT NULL,
                    run_dir TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(session_name, run_id, started_at)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_issue_runs_issue "
                "ON issue_runs(issue_number, recorded_at)"
            )

    def _validate_existing(self) -> None:
        try:
            with closing(self._connect()) as conn:
                identities = conn.execute("SELECT identity FROM issue_run_ledger_identity").fetchall()
                if [row[0] for row in identities] != [self._identity]:
                    raise IssueRunEvidenceUnavailable("Run ledger initialization identity changed")
                conn.execute(
                    "SELECT session_name, run_id, started_at, issue_number, issue_scope, "
                    "issue_key, task, assets_json, run_dir, recorded_at FROM issue_runs LIMIT 0"
                )
        except sqlite3.Error as exc:
            raise IssueRunEvidenceUnavailable("Established run ledger schema is unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        if not self._path.is_file():
            raise IssueRunEvidenceUnavailable(f"Run ledger disappeared: {self._path}")
        return open_sqlite(self._path, row_factory=sqlite3.Row)

    def record_run(self, issue_number: int, record: IssueRunRecord) -> None:
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("run ownership requires a positive issue number")
        identity = record.run.identity
        payload = (
            issue_number,
            record.session_key.issue.scope(),
            record.session_key.issue.stable_id(),
            record.session_key.task.value,
            json.dumps(record.run.to_dict(), sort_keys=True, separators=(",", ":")),
        )
        key = (identity.session_name, identity.run_id, identity.started_at)
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT issue_number, issue_scope, issue_key, task, assets_json "
                    "FROM issue_runs WHERE session_name=? AND run_id=? AND started_at=?", key,
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != payload:
                        raise IssueRunEvidenceUnavailable(
                            f"Conflicting ownership for run {identity}"
                        )
                    return
                conn.execute(
                    "INSERT INTO issue_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*key, *payload, str(record.run.run_dir.resolve()), record.recorded_at),
                )
        except sqlite3.Error as exc:
            raise IssueRunEvidenceUnavailable("Could not persist run ownership") from exc

    def recorded_runs(self, issue_number: int) -> tuple[IssueRunRecord, ...]:
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT * FROM issue_runs WHERE issue_number=? "
                    "ORDER BY recorded_at, session_name, run_id, started_at", (issue_number,),
                ).fetchall()
            return tuple(self._decode(row) for row in rows)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise IssueRunEvidenceUnavailable("Could not read run ownership") from exc

    @staticmethod
    def _decode(row: sqlite3.Row) -> IssueRunRecord:
        payload = json.loads(row["assets_json"])
        if not isinstance(payload, dict):
            raise ValueError("Run ledger assets must be an object")
        assets = SessionRunAssets.from_dict(payload)
        if (assets.session_name, assets.run_id, assets.started_at) != (
            row["session_name"], row["run_id"], row["started_at"],
        ):
            raise ValueError("Run ledger key and assets disagree")
        if str(assets.run_dir.resolve()) != row["run_dir"]:
            raise ValueError("Run ledger root and assets disagree")
        return IssueRunRecord(
            session_key=SessionKey(
                issue=GitHubIssueKey(repo=row["issue_scope"], external_id=row["issue_key"]),
                task=TaskKind(row["task"]),
            ),
            run=assets,
            recorded_at=row["recorded_at"],
        )
