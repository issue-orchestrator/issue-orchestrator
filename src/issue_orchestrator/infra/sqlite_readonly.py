"""Bounded no-create/application-read-only SQLite access."""

from __future__ import annotations

import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from math import ceil, isfinite
from pathlib import Path
from typing import Callable

from ..domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)


def open_sqlite_readonly(
    path: Path,
    *,
    timeout: float,
    row_factory: Callable[..., object],
) -> sqlite3.Connection:
    """Open an existing main database with the shared no-create/read-only profile."""
    _require_row_factory(row_factory)
    resolved, deadline = _readonly_parameters(path, timeout)
    return _open_sqlite_readonly(
        resolved,
        timeout=timeout,
        row_factory=row_factory,
        deadline=deadline,
    )


@contextmanager
def readonly_sqlite_transaction(
    path: Path,
    *,
    timeout: float,
    row_factory: Callable[..., object],
) -> Iterator[sqlite3.Connection]:
    """Own one bounded read transaction and classify every SQLite failure."""
    _require_row_factory(row_factory)
    resolved, deadline = _readonly_parameters(path, timeout)
    connection = _open_sqlite_readonly(
        resolved,
        timeout=timeout,
        row_factory=row_factory,
        deadline=deadline,
    )
    try:
        _set_remaining_busy_timeout(connection, resolved, deadline)
        connection.execute("BEGIN")
        _require_time_remaining(resolved, deadline)
        yield connection
        _require_time_remaining(resolved, deadline)
    except ReadOnlySqliteAccessError:
        raise
    except sqlite3.Error as error:
        raise _readonly_error(resolved, deadline, error) from error
    finally:
        connection.set_progress_handler(None, 0)
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _open_sqlite_readonly(
    resolved: Path,
    *,
    timeout: float,
    row_factory: Callable[..., object],
    deadline: float,
) -> sqlite3.Connection:
    _require_database_file(resolved)
    connection: sqlite3.Connection | None = None
    try:
        _require_time_remaining(resolved, deadline)
        connection = sqlite3.connect(
            resolved.as_uri() + "?mode=ro&cache=private",
            uri=True,
            timeout=_remaining_seconds(resolved, deadline),
        )
        _require_time_remaining(resolved, deadline)
        connection.row_factory = row_factory
        _set_remaining_busy_timeout(connection, resolved, deadline)
        connection.execute("PRAGMA query_only=ON")
        _require_time_remaining(resolved, deadline)
        connection.execute("PRAGMA temp_store=MEMORY")
        _require_time_remaining(resolved, deadline)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        return connection
    except ReadOnlySqliteAccessError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise _readonly_error(resolved, deadline, error) from error


def _require_row_factory(row_factory: Callable[..., object]) -> None:
    if not callable(row_factory):
        raise TypeError("read-only SQLite row factory must be callable")


def _readonly_parameters(path: Path, timeout: float) -> tuple[Path, float]:
    if not isinstance(path, Path):
        raise TypeError("read-only SQLite path must be a Path")
    if type(timeout) not in {int, float} or not isfinite(timeout) or timeout <= 0:
        raise ValueError("read-only SQLite timeout must be finite and positive")
    return path.resolve(), time.monotonic() + timeout


def _readonly_error(
    path: Path, deadline: float, error: sqlite3.Error
) -> ReadOnlySqliteAccessError:
    if _database_absent(path):
        reason = ReadOnlySqliteFailure.DATABASE_ABSENT
    elif time.monotonic() >= deadline:
        reason = ReadOnlySqliteFailure.TIMEOUT
    else:
        reason = ReadOnlySqliteFailure.UNREADABLE
    return ReadOnlySqliteAccessError(reason, f"SQLite read failed: {error}")


def _remaining_seconds(path: Path, deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise_timeout(path)
    return remaining


def _require_time_remaining(path: Path, deadline: float) -> None:
    if time.monotonic() >= deadline:
        _raise_timeout(path)


def _set_remaining_busy_timeout(
    connection: sqlite3.Connection, path: Path, deadline: float
) -> None:
    remaining_ms = ceil(_remaining_seconds(path, deadline) * 1000)
    connection.execute(f"PRAGMA busy_timeout={remaining_ms}")


def _raise_timeout(path: Path) -> None:
    raise ReadOnlySqliteAccessError(
        ReadOnlySqliteFailure.TIMEOUT,
        f"SQLite read deadline expired: {path}",
    )


def _require_database_file(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.DATABASE_ABSENT,
            f"SQLite database is absent: {path}",
        ) from error
    except OSError as error:
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.UNREADABLE,
            f"SQLite database cannot be inspected: {error}",
        ) from error
    if not stat.S_ISREG(mode):
        raise ReadOnlySqliteAccessError(
            ReadOnlySqliteFailure.UNREADABLE,
            f"SQLite database path is not a regular file: {path}",
        )


def _database_absent(path: Path) -> bool:
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False
