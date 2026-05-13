"""SQLite schema for E2E run persistence."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS e2e_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_root TEXT NOT NULL,
    orchestrator_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    pytest_args TEXT NOT NULL,
    command_json TEXT NOT NULL DEFAULT '[]',
    runner_kind TEXT NOT NULL DEFAULT 'pytest',
    commit_sha TEXT,
    branch TEXT,
    retry_of INTEGER,
    is_retry_run INTEGER DEFAULT 0,
    duration_seconds REAL,
    note TEXT,
    log_path TEXT,
    artifacts_dir TEXT,
    worker_pid INTEGER,
    total_tests INTEGER,
    current_test TEXT,
    orchestrator_instance_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS e2e_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    nodeid TEXT NOT NULL,
    display_name TEXT,
    suite_name TEXT,
    result_source TEXT NOT NULL DEFAULT 'runtime',
    stdout_available INTEGER NOT NULL DEFAULT 0,
    stderr_available INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,
    duration_seconds REAL,
    longrepr TEXT,
    retry_outcome TEXT,
    is_quarantined INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_runs_orch_started
    ON e2e_runs(orchestrator_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_e2e_test_results_run
    ON e2e_test_results(run_id, outcome);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_test_results_run_nodeid
    ON e2e_test_results(run_id, nodeid);

CREATE TABLE IF NOT EXISTS e2e_run_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_run_artifacts_run
    ON e2e_run_artifacts(run_id);

-- E2E Issue Tracking: Links test failures to GitHub issues
CREATE TABLE IF NOT EXISTS e2e_failure_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nodeid TEXT NOT NULL,
    github_issue_number INTEGER NOT NULL,
    parent_issue_number INTEGER NOT NULL,
    first_failing_run_id INTEGER NOT NULL,
    first_failing_sha TEXT NOT NULL,
    last_passing_sha TEXT,
    resolved_at TEXT,
    resolution TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(first_failing_run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_failure_issues_nodeid_sha
    ON e2e_failure_issues(nodeid, first_failing_sha);

CREATE INDEX IF NOT EXISTS idx_e2e_failure_issues_parent
    ON e2e_failure_issues(parent_issue_number);

-- E2E Issue Tracking: Tracks E2E run parent issues
CREATE TABLE IF NOT EXISTS e2e_run_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    github_issue_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_run_issues_run
    ON e2e_run_issues(run_id);

-- E2E Flakiness Tracking: Records flaky test occurrences
CREATE TABLE IF NOT EXISTS e2e_flake_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nodeid TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    was_flaky INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES e2e_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_e2e_flake_history_nodeid
    ON e2e_flake_history(nodeid, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_e2e_test_results_nodeid
    ON e2e_test_results(nodeid);

"""
