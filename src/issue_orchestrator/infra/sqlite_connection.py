"""Centralized SQLite connection helpers."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

from .sqlite_readonly import (
    open_sqlite_readonly as open_sqlite_readonly,
    readonly_sqlite_transaction as readonly_sqlite_transaction,
)


def open_sqlite(
    path: Path | str,
    *,
    timeout: float | None = None,
    check_same_thread: bool | None = None,
    isolation_level: str | None = None,
    row_factory: Callable | None = None,
    pragmas: bool = True,
    uri: bool = False,
) -> sqlite3.Connection:
    """Open a SQLite connection with consistent pragmas and options."""
    kwargs: dict = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if check_same_thread is not None:
        kwargs["check_same_thread"] = check_same_thread
    if isolation_level is not None:
        kwargs["isolation_level"] = isolation_level

    conn = sqlite3.connect(str(path), uri=uri, **kwargs)
    if row_factory is not None:
        conn.row_factory = row_factory
    if pragmas:
        apply_sqlite_pragmas(conn)
    return conn


def apply_sqlite_pragmas(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = 5000,
) -> None:
    """Apply durability pragmas."""
    busy_timeout_ms = _positive_pragma_integer(busy_timeout_ms)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    for attempt in range(5):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.1 * (2**attempt))
    conn.execute("PRAGMA synchronous = FULL")


def _positive_pragma_integer(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("SQLite pragma value must be a positive integer")
    return value
