"""SQLite adapter for the rebuildable tech-lead open-issue corpus cache."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ..domain.open_issue_corpus import OpenIssueFingerprint, OpenIssueRef
from ..ports.open_issue_corpus_store import (
    OpenIssueCorpusSnapshot,
    index_open_issue_entries,
    validate_corpus_watermark,
    validate_open_issue_evictions,
)
from .repo_identity import state_dir
from .sqlite_connection import open_sqlite

_WATERMARK_KEY = "delta_watermark"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS open_issue_fingerprints (
    issue_number INTEGER PRIMARY KEY,
    normalized_title TEXT NOT NULL,
    normalized_body TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS open_issue_corpus_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteOpenIssueCorpusStore:
    """Persists normalized open issues and their successful delta cursor."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqliteOpenIssueCorpusStore":
        return cls(state_dir(repo_root) / "open_issue_corpus.sqlite")

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._get_connection().executescript(_SCHEMA)

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

    def load(self) -> OpenIssueCorpusSnapshot | None:
        conn = self._get_connection()
        meta = conn.execute(
            "SELECT value FROM open_issue_corpus_meta WHERE key = ?",
            (_WATERMARK_KEY,),
        ).fetchone()
        if meta is None:
            return None
        rows = conn.execute(
            "SELECT issue_number, normalized_title, normalized_body, "
            "content_fingerprint FROM open_issue_fingerprints "
            "ORDER BY issue_number"
        ).fetchall()
        return OpenIssueCorpusSnapshot(
            entries=tuple(
                OpenIssueFingerprint(
                    issue=OpenIssueRef(
                        int(row["issue_number"]),
                        str(row["normalized_title"]),
                        str(row["normalized_body"]),
                    ),
                    content_fingerprint=str(row["content_fingerprint"]),
                )
                for row in rows
            ),
            watermark=str(meta["value"]),
        )

    def replace_all(
        self,
        entries: Sequence[OpenIssueFingerprint],
        *,
        watermark: str,
    ) -> None:
        entry_map = index_open_issue_entries(entries)
        cursor = validate_corpus_watermark(watermark)
        with self._transaction() as tx:
            tx.execute("DELETE FROM open_issue_fingerprints")
            self._upsert(tx, entry_map.values())
            self._save_watermark(tx, cursor)

    def apply_delta(
        self,
        upserts: Sequence[OpenIssueFingerprint],
        *,
        evict_issue_numbers: Sequence[int],
        watermark: str,
    ) -> None:
        upsert_map = index_open_issue_entries(upserts)
        evictions = validate_open_issue_evictions(evict_issue_numbers)
        overlap = set(upsert_map).intersection(evictions)
        if overlap:
            raise ValueError(
                f"open-issue delta both upserts and evicts issue(s): {sorted(overlap)}"
            )
        cursor = validate_corpus_watermark(watermark)
        with self._transaction() as tx:
            self._upsert(tx, upsert_map.values())
            tx.executemany(
                "DELETE FROM open_issue_fingerprints WHERE issue_number = ?",
                ((issue_number,) for issue_number in evictions),
            )
            self._save_watermark(tx, cursor)

    @staticmethod
    def _upsert(
        tx: sqlite3.Connection,
        entries: Iterable[OpenIssueFingerprint],
    ) -> None:
        tx.executemany(
            "INSERT INTO open_issue_fingerprints "
            "(issue_number, normalized_title, normalized_body, content_fingerprint) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(issue_number) DO UPDATE SET "
            "normalized_title = excluded.normalized_title, "
            "normalized_body = excluded.normalized_body, "
            "content_fingerprint = excluded.content_fingerprint "
            "WHERE open_issue_fingerprints.content_fingerprint "
            "<> excluded.content_fingerprint",
            (
                (
                    entry.issue.number,
                    entry.issue.title,
                    entry.issue.body,
                    entry.content_fingerprint,
                )
                for entry in entries
            ),
        )

    @staticmethod
    def _save_watermark(tx: sqlite3.Connection, watermark: str) -> None:
        tx.execute(
            "INSERT INTO open_issue_corpus_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_WATERMARK_KEY, watermark),
        )
