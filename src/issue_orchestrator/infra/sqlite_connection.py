"""Centralized SQLite connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable



def open_sqlite(
    path: Path | str,
    *,
    timeout: float | None = None,
    check_same_thread: bool | None = None,
    isolation_level: str | None = None,
    row_factory: Callable | None = None,
    pragmas: bool = True,
) -> sqlite3.Connection:
    """Open a SQLite connection with consistent pragmas and options."""
    kwargs: dict = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if check_same_thread is not None:
        kwargs["check_same_thread"] = check_same_thread
    if isolation_level is not None:
        kwargs["isolation_level"] = isolation_level

    conn = sqlite3.connect(str(path), **kwargs)
    if row_factory is not None:
        conn.row_factory = row_factory
    if pragmas:
        _apply_pragmas(conn)
    return conn


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply durability pragmas."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
