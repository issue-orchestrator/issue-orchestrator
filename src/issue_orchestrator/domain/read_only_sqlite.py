"""Typed failures for bounded application read-only SQLite access."""

from __future__ import annotations

from enum import StrEnum


class ReadOnlySqliteFailure(StrEnum):
    DATABASE_ABSENT = "database_absent"
    UNREADABLE = "unreadable"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    TIMEOUT = "timeout"


class ReadOnlySqliteAccessError(RuntimeError):
    """A read boundary failure whose category is safe for callers to branch on."""

    reason: ReadOnlySqliteFailure

    def __init__(self, reason: ReadOnlySqliteFailure, message: str) -> None:
        if type(reason) is not ReadOnlySqliteFailure:
            raise ValueError("read failure requires a typed reason")
        if type(message) is not str or not message.strip():
            raise ValueError("read failure requires an explanation")
        self.reason = reason
        super().__init__(message)
