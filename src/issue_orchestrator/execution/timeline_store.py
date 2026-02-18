"""Filesystem-backed timeline store."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Iterable, Iterator
from uuid import uuid4

from .timeline_artifact_expectations import RUN_SCOPED_TIMELINE_EVENTS, event_requires_run_dir
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..ports.timeline_store import TimelineRecord, TimelineStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimelineStoreConfig:
    max_records: int = 5000
    max_total_records: int = 250000


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    run_dir TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL,
    CHECK (
        event NOT IN ({run_scoped_events})
        OR length(trim(run_dir)) > 0
    )
);

CREATE INDEX IF NOT EXISTS idx_timeline_issue_sequence
    ON timeline_events(issue_number, sequence DESC);

CREATE INDEX IF NOT EXISTS idx_timeline_issue_event_run_dir
    ON timeline_events(issue_number, event, run_dir);
"""

_SQLITE_SCHEMA_VERSION = 2


def _quoted_csv(values: Iterable[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


def _sqlite_schema_sql() -> str:
    return _SQLITE_SCHEMA.format(run_scoped_events=_quoted_csv(RUN_SCOPED_TIMELINE_EVENTS))


class SqliteTimelineStore(TimelineStore):
    """SQLite-backed timeline store."""

    def __init__(self, db_path: Path, config: TimelineStoreConfig | None = None) -> None:
        self._db_path = db_path
        self._config = config or TimelineStoreConfig()
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._db_inode: int | None = None
        self.initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        self._ensure_schema(conn)
        self._db_inode = self._capture_db_inode()
        if _timeline_trace_enabled():
            logger.info(
                "[TIMELINE] trace enabled db=%s inode=%s",
                self._db_path,
                self._db_inode,
            )

    def _get_connection(self) -> sqlite3.Connection:
        self._assert_db_file_unchanged()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(self._db_path, row_factory=sqlite3.Row)
            self._ensure_schema(conn)
            self._local.conn = conn
            if self._db_inode is None:
                self._db_inode = self._capture_db_inode()
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        user_version = int(version_row[0]) if version_row else 0
        if user_version != _SQLITE_SCHEMA_VERSION:
            # No backwards-compatibility path: rebuild timeline table to enforce invariants.
            conn.executescript(
                """
                DROP TABLE IF EXISTS timeline_events;
                DROP INDEX IF EXISTS idx_timeline_issue_sequence;
                DROP INDEX IF EXISTS idx_timeline_issue_event_run_dir;
                """
            )
            conn.executescript(_sqlite_schema_sql())
            conn.execute(f"PRAGMA user_version = {_SQLITE_SCHEMA_VERSION}")
            conn.commit()
            return
        conn.executescript(_sqlite_schema_sql())

    def _capture_db_inode(self) -> int | None:
        try:
            return os.stat(self._db_path).st_ino
        except FileNotFoundError:
            return None

    def _assert_db_file_unchanged(self) -> None:
        expected = self._db_inode
        if expected is None:
            return
        current = self._capture_db_inode()
        if current is None:
            raise RuntimeError(
                f"Timeline DB missing on disk: {self._db_path}. "
                "Database file was removed while store is active."
            )
        if current != expected:
            raise RuntimeError(
                f"Timeline DB replaced on disk: {self._db_path} "
                f"(expected inode={expected}, current inode={current}). "
                "Failing fast to avoid split-brain timeline writes."
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        run_dir_value = record.data.get("run_dir")
        run_dir = run_dir_value.strip() if isinstance(run_dir_value, str) else ""
        if event_requires_run_dir(record.event) and not run_dir:
            raise RuntimeError(
                f"timeline DB invariant failed: event={record.event} requires non-empty run_dir"
            )
        payload = json.dumps(record.data, sort_keys=True, default=str)
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO timeline_events (issue_number, event_id, timestamp, event, run_dir, data_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (issue_number, record.event_id, record.timestamp, record.event, run_dir, payload),
            )
            self._trim_if_needed(tx, issue_number)
            self._trim_total_if_needed(tx)
        if _timeline_trace_enabled():
            logger.info(
                "[TIMELINE] append db=%s issue=%s event=%s event_id=%s",
                self._db_path,
                issue_number,
                record.event,
                record.event_id,
            )

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        conn = self._get_connection()
        if limit is not None and limit > 0:
            rows = conn.execute(
                """
                SELECT event_id, timestamp, event, data_json
                FROM timeline_events
                WHERE issue_number = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (issue_number, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                """
                SELECT event_id, timestamp, event, data_json
                FROM timeline_events
                WHERE issue_number = ?
                ORDER BY sequence ASC
                """,
                (issue_number,),
            ).fetchall()

        records: list[TimelineRecord] = []
        for row in rows:
            data_json = row["data_json"] or "{}"
            try:
                data = json.loads(data_json)
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            records.append(
                TimelineRecord(
                    event_id=str(row["event_id"]),
                    timestamp=str(row["timestamp"]),
                    event=str(row["event"]),
                    data=data,
                )
            )
        if _timeline_trace_enabled():
            logger.info(
                "[TIMELINE] read db=%s issue=%s count=%s limit=%s",
                self._db_path,
                issue_number,
                len(records),
                limit,
            )
        return records

    def _trim_if_needed(self, conn: sqlite3.Connection, issue_number: int) -> None:
        max_records = self._config.max_records
        if max_records <= 0:
            return
        before_count = 0
        if _timeline_trace_enabled():
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM timeline_events WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            before_count = int(row["count"]) if row else 0

        conn.execute(
            """
            DELETE FROM timeline_events
            WHERE issue_number = ?
              AND sequence NOT IN (
                SELECT sequence
                FROM timeline_events
                WHERE issue_number = ?
                ORDER BY sequence DESC
                LIMIT ?
              )
            """,
            (issue_number, issue_number, max_records),
        )
        if _timeline_trace_enabled():
            after_row = conn.execute(
                "SELECT COUNT(*) AS count FROM timeline_events WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            after_count = int(after_row["count"]) if after_row else 0
            deleted = max(0, before_count - after_count)
            logger.info(
                "[TIMELINE] trim_issue db=%s issue=%s max=%s before=%s after=%s deleted=%s",
                self._db_path,
                issue_number,
                max_records,
                before_count,
                after_count,
                deleted,
            )

    def _trim_total_if_needed(self, conn: sqlite3.Connection) -> None:
        max_total_records = self._config.max_total_records
        if max_total_records <= 0:
            return
        before_count = 0
        if _timeline_trace_enabled():
            row = conn.execute("SELECT COUNT(*) AS count FROM timeline_events").fetchone()
            before_count = int(row["count"]) if row else 0

        conn.execute(
            """
            DELETE FROM timeline_events
            WHERE sequence NOT IN (
                SELECT sequence
                FROM timeline_events
                ORDER BY sequence DESC
                LIMIT ?
            )
            """,
            (max_total_records,),
        )
        if _timeline_trace_enabled():
            after_row = conn.execute("SELECT COUNT(*) AS count FROM timeline_events").fetchone()
            after_count = int(after_row["count"]) if after_row else 0
            deleted = max(0, before_count - after_count)
            logger.info(
                "[TIMELINE] trim_total db=%s max=%s before=%s after=%s deleted=%s",
                self._db_path,
                max_total_records,
                before_count,
                after_count,
                deleted,
            )

class FileSystemTimelineStore(TimelineStore):
    """Append-only JSONL timeline store per issue."""

    def __init__(self, repo_root: Path, config: TimelineStoreConfig | None = None):
        self._root = state_dir(repo_root) / "timeline"
        self._root.mkdir(parents=True, exist_ok=True)
        self._config = config or TimelineStoreConfig()

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        path = self._issue_path(issue_number)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.__dict__, sort_keys=True, default=str) + "\n")
        self._trim_if_needed(path)

    def append_event(self, issue_number: int, event: str, data: dict) -> None:
        record = TimelineRecord(
            event_id=str(uuid4()),
            timestamp=_now_iso(),
            event=event,
            data=data,
        )
        self.append(issue_number, record)

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        path = self._issue_path(issue_number)
        if not path.exists():
            return []
        return list(_load_records(path, limit=limit))

    def _issue_path(self, issue_number: int) -> Path:
        return self._root / f"issue-{issue_number}.jsonl"

    def _trim_if_needed(self, path: Path) -> None:
        max_records = self._config.max_records
        if max_records <= 0 or not path.exists():
            return
        buffer: deque[str] = deque(maxlen=max_records)
        count = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                count += 1
                buffer.append(line)
        if count <= max_records:
            return
        if count <= max_records * 2:
            return
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(buffer)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timeline_trace_enabled() -> bool:
    return is_timeline_trace_enabled()


def _load_records(path: Path, limit: int | None = None) -> Iterable[TimelineRecord]:
    lines: Iterable[str]
    if limit is not None and limit > 0:
        buffer: deque[str] = deque(maxlen=limit)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    buffer.append(line)
        lines = list(buffer)
    else:
        with path.open("r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
    for line in lines:
        payload = json.loads(line)
        yield TimelineRecord(
            event_id=payload.get("event_id", ""),
            timestamp=payload.get("timestamp", ""),
            event=payload.get("event", ""),
            data=payload.get("data") or {},
        )


class TimelineIssueLocator:
    """Persist issue -> repo-root mapping for timeline ownership."""

    def __init__(self, default_repo_root: Path):
        self._default_repo_root = default_repo_root.resolve()
        self._mapping_path = state_dir(self._default_repo_root) / "timeline_issue_locations.json"
        self._mapping_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, str] | None = None

    def get_repo_root(self, issue_number: int) -> Path | None:
        raw = self._load().get(str(issue_number))
        if not raw:
            return None
        return Path(raw)

    def bind_repo_root(self, issue_number: int, repo_root: Path) -> None:
        resolved = repo_root.resolve()
        mapping = self._load()
        key = str(issue_number)
        if mapping.get(key) == str(resolved):
            return
        mapping[key] = str(resolved)
        self._save(mapping)

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self._mapping_path.exists():
            self._cache = {}
            return self._cache
        try:
            payload = json.loads(self._mapping_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._cache = {}
            return self._cache
        if not isinstance(payload, dict):
            self._cache = {}
            return self._cache
        self._cache = {str(k): str(v) for k, v in payload.items()}
        return self._cache

    def _save(self, mapping: dict[str, str]) -> None:
        tmp = self._mapping_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mapping, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._mapping_path)
        self._cache = dict(mapping)


class RoutedTimelineStore(TimelineStore):
    """Route timeline read/write operations to the authoritative issue repo root."""

    def __init__(
        self,
        default_repo_root: Path,
        config: TimelineStoreConfig | None = None,
        locator: TimelineIssueLocator | None = None,
    ):
        self._default_repo_root = default_repo_root.resolve()
        self._config = config or TimelineStoreConfig()
        self._locator = locator or TimelineIssueLocator(self._default_repo_root)
        self._stores: dict[Path, FileSystemTimelineStore] = {}

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        owner_root = self._infer_repo_root(record.data) or self._locator.get_repo_root(issue_number) or self._default_repo_root
        self._locator.bind_repo_root(issue_number, owner_root)
        self._store_for(owner_root).append(issue_number, record)

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        owner_root = self._locator.get_repo_root(issue_number) or self._default_repo_root
        return self._store_for(owner_root).read(issue_number, limit=limit)

    def owner_repo_root(self, issue_number: int) -> Path:
        """Return the current authoritative repo root for an issue timeline."""
        return self._locator.get_repo_root(issue_number) or self._default_repo_root

    def _store_for(self, repo_root: Path) -> FileSystemTimelineStore:
        key = repo_root.resolve()
        store = self._stores.get(key)
        if store is None:
            store = FileSystemTimelineStore(key, config=self._config)
            self._stores[key] = store
        return store

    @staticmethod
    def _infer_repo_root(data: dict[str, object]) -> Path | None:
        worktree_path = data.get("worktree_path")
        if isinstance(worktree_path, str) and worktree_path:
            return Path(worktree_path).resolve()

        run_dir = data.get("run_dir")
        if isinstance(run_dir, str) and run_dir:
            run_path = Path(run_dir).resolve()
            parts = run_path.parts
            if ".issue-orchestrator" in parts:
                idx = parts.index(".issue-orchestrator")
                if idx > 0:
                    return Path(*parts[:idx]).resolve()
        return None
