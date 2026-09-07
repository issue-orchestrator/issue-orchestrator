"""SQLite shape owned by the durable issue-run ledger, including role migration."""

import sqlite3


def create_issue_run_schema(conn: sqlite3.Connection, identity: str) -> None:
    conn.execute("CREATE TABLE issue_run_ledger_identity (identity TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO issue_run_ledger_identity VALUES (?)", (identity,))
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
            branch_name TEXT,
            terminal_binding TEXT,
            agent_label TEXT,
            completion_task TEXT,
            PRIMARY KEY(session_name, run_id, started_at)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_issue_runs_issue "
        "ON issue_runs(issue_number, recorded_at)"
    )


def validate_issue_run_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "SELECT session_name, run_id, started_at, issue_number, issue_scope, "
        "issue_key, task, assets_json, run_dir, recorded_at FROM issue_runs LIMIT 0"
    )
    # Existing facts stay unchanged: absent roles remain explicitly
    # unknown and cannot authorize receipt processing.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(issue_runs)")}
    if "agent_label" not in columns:
        conn.execute("ALTER TABLE issue_runs ADD COLUMN agent_label TEXT")
    if "completion_task" not in columns:
        conn.execute("ALTER TABLE issue_runs ADD COLUMN completion_task TEXT")
    if "branch_name" not in columns:
        conn.execute("ALTER TABLE issue_runs ADD COLUMN branch_name TEXT")
    if "terminal_binding" not in columns:
        conn.execute("ALTER TABLE issue_runs ADD COLUMN terminal_binding TEXT")
