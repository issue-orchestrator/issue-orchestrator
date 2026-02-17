"""Filesystem-backed timeline store."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Iterable, Iterator
from uuid import uuid4

from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..ports.timeline_store import TimelineRecord, TimelineStore


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
    data_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timeline_issue_sequence
    ON timeline_events(issue_number, sequence DESC);
"""


class SqliteTimelineStore(TimelineStore):
    """SQLite-backed timeline store."""

    def __init__(self, db_path: Path, config: TimelineStoreConfig | None = None) -> None:
        self._db_path = db_path
        self._config = config or TimelineStoreConfig()
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(_SQLITE_SCHEMA)
        self._migrate_legacy_jsonl_if_needed(conn)

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(self._db_path, row_factory=sqlite3.Row)
            self._local.conn = conn
        return conn

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
        payload = json.dumps(record.data, sort_keys=True, default=str)
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO timeline_events (issue_number, event_id, timestamp, event, data_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (issue_number, record.event_id, record.timestamp, record.event, payload),
            )
            self._trim_if_needed(tx, issue_number)
            self._trim_total_if_needed(tx)

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
        return records

    def _trim_if_needed(self, conn: sqlite3.Connection, issue_number: int) -> None:
        max_records = self._config.max_records
        if max_records <= 0:
            return
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

    def _trim_total_if_needed(self, conn: sqlite3.Connection) -> None:
        max_total_records = self._config.max_total_records
        if max_total_records <= 0:
            return
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

    def _migrate_legacy_jsonl_if_needed(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute("SELECT COUNT(*) AS count FROM timeline_events").fetchone()
        if existing is not None and int(existing["count"]) > 0:
            return

        legacy_dir = self._db_path.parent / "timeline"
        if not legacy_dir.exists():
            return

        issue_files = sorted(legacy_dir.glob("issue-*.jsonl"))
        if not issue_files:
            return

        for issue_file in issue_files:
            issue_number = _issue_number_from_filename(issue_file)
            if issue_number is None:
                continue
            for record in _load_records(issue_file):
                payload = json.dumps(record.data, sort_keys=True, default=str)
                conn.execute(
                    """
                    INSERT INTO timeline_events (issue_number, event_id, timestamp, event, data_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        issue_number,
                        record.event_id,
                        record.timestamp,
                        record.event,
                        payload,
                    ),
                )
            self._trim_if_needed(conn, issue_number)

        self._trim_total_if_needed(conn)
        conn.commit()


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


_ISSUE_TIMELINE_FILE_RE = re.compile(r"^issue-(\d+)\.jsonl$")


def _issue_number_from_filename(path: Path) -> int | None:
    match = _ISSUE_TIMELINE_FILE_RE.match(path.name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


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
