"""Focused contracts for configurable shared SQLite durability pragmas."""

from __future__ import annotations

import sqlite3

import pytest

from issue_orchestrator.infra.sqlite_connection import apply_sqlite_pragmas


def test_shared_pragmas_honor_the_callers_busy_timeout() -> None:
    connection = sqlite3.connect(":memory:")

    apply_sqlite_pragmas(connection, busy_timeout_ms=37)

    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 37
    connection.close()


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_shared_pragmas_reject_invalid_busy_timeouts(value: object) -> None:
    connection = sqlite3.connect(":memory:")

    with pytest.raises(ValueError, match="pragma value"):
        apply_sqlite_pragmas(connection, busy_timeout_ms=value)  # type: ignore[arg-type]

    connection.close()
