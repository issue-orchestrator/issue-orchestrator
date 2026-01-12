# Async E2E Test Runner

The Issue Orchestrator includes a built-in facility for running end-to-end tests asynchronously, with full visibility in the web dashboard and robust handling of long-running test suites.

## Overview

The E2E runner executes pytest-based tests in a background worker process, storing results in SQLite for persistence across restarts. It's designed for test suites that take minutes to hours, with features specifically aimed at reducing flakiness and providing visibility into test execution.

## Quick Start

### 1. Enable in config

```yaml
# .issue-orchestrator/config/default.yaml
e2e:
  enabled: true
  auto_run_interval_minutes: 30    # Auto-run after agent work (0 = manual only)
  pytest_args: ["tests/e2e", "-v"]
  allow_retry_once: true
  quarantine_file: "tests/e2e/quarantine.txt"
  survive_restart: true
```

### 2. Run manually or let it auto-trigger

**Manual:** Click "Run E2E" in the dashboard, or:
```bash
curl -X POST http://localhost:8080/control/e2e/start \
  -H "Content-Type: application/json" \
  -d '{"repo_root": "'$(pwd)'"}'
```

**Auto-trigger:** When `auto_run_interval_minutes > 0`, E2E runs automatically after agent sessions complete (respecting the interval).

### 3. Monitor in dashboard

The E2E panel shows:
- Live progress bar with test counts
- Current test being executed
- Signal score (pass rate over recent runs)
- Quarantine count (click to view list)

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `false` | Enable the E2E runner |
| `auto_run_interval_minutes` | int | `30` | Minimum minutes between auto-triggered runs. Set to 0 to disable auto-trigger. |
| `pytest_args` | list | `["tests/e2e", "-v"]` | Arguments passed to pytest |
| `allow_retry_once` | bool | `true` | Automatically retry failed tests once |
| `quarantine_file` | string | `"tests/e2e/quarantine.txt"` | Path to quarantine list (relative to repo root) |
| `survive_restart` | bool | `true` | Let E2E worker continue if orchestrator restarts |

## Features

### Progress Tracking

The runner tracks test execution in real-time:
- **Total tests**: Set after pytest collection phase
- **Current test**: Updated as each test starts
- **Completed counts**: Passed, failed, skipped updated after each test

The dashboard polls every 2 seconds while E2E is running, showing a live progress bar.

### Resumable Runs

If the orchestrator restarts mid-run (or the E2E worker is interrupted):

1. The interrupted run is detected via orphan PID detection
2. On next start, the runner checks for interrupted runs
3. Tests that already passed are skipped via `--deselect`
4. Execution resumes from where it left off

**Best practice for resumability:** Structure tests as discrete functions rather than monolithic test cases:

```python
# Good - each function is a resumable checkpoint
def test_create_issue(): ...
def test_create_pr(): ...
def test_review_cycle(): ...

# Bad - monolithic, no partial progress saved
def test_entire_workflow():
    # 20 minutes of steps with no checkpoints
    ...
```

### Retry-Once Policy

When `allow_retry_once: true`:

1. After the initial run, failed tests (non-quarantined) are collected
2. Each failed test is re-run individually
3. Results are recorded as `retry_outcome` (passed/failed)
4. Final status is "passed" only if all retries pass

This reduces flakiness impact without masking real failures.

### Quarantine Support

The quarantine file lists known-flaky tests that should be excluded from failure counts:

```
# tests/e2e/quarantine.txt
# Known flaky tests - still run but excluded from required pass
tests/e2e/test_slow_network.py::test_timeout_handling
tests/e2e/test_race_condition.py::test_concurrent_updates
```

Quarantined tests:
- Still execute (so you know if they start passing)
- Are marked with `is_quarantined=1` in the database
- Don't affect the overall pass/fail status
- Are shown separately in the dashboard

### Signal Score

Track E2E stability over time:

```
Stability: 94% (30 runs) · 2 quarantined
```

The signal score shows:
- **Pass rate**: Percentage of recent runs that passed
- **Runs analyzed**: How many runs are in the calculation (up to 30)
- **Quarantine count**: Tests currently in quarantine

### Survive Restart

When `survive_restart: true`:
- The E2E worker runs in its own process group
- If the orchestrator restarts, the worker continues
- On next startup, the orchestrator detects the running worker
- Progress is preserved and visible in the dashboard

## API Reference

### Start E2E Run

```bash
POST /control/e2e/start
Content-Type: application/json

{
  "repo_root": "/path/to/repo",
  "pytest_args": ["tests/e2e", "-v"],  # optional override
  "allow_retry_once": true              # optional override
}
```

**Response (success):**
```json
{"status": "started", "pid": 12345, "log_path": ".issue-orchestrator/logs/e2e/run_20240115_143022.log"}
```

**Response (already running):**
```json
{"error": "already_running", "pid": 12345}
```

In the dashboard, if you click "Run E2E" while tests are running, you'll be asked if you want to cancel and restart.

### Stop E2E Run

```bash
POST /control/e2e/stop
Content-Type: application/json

{"repo_root": "/path/to/repo"}
```

### Get Status

```bash
GET /control/e2e/status?repo_root=/path/to/repo
```

**Response:**
```json
{
  "enabled": true,
  "running": true,
  "pid": 12345,
  "last_run": {
    "id": 5,
    "status": "running",
    "started_at": "2024-01-15T14:30:22Z",
    "total_tests": 19,
    "commit_sha": "abc123f",
    "branch": "main"
  },
  "progress": {
    "total_tests": 19,
    "completed": 12,
    "passed": 10,
    "failed": 2,
    "skipped": 0,
    "current_test": "tests/e2e/test_pipeline.py::test_review_cycle",
    "percent": 63
  },
  "signal_score": {
    "pass_rate": 0.94,
    "runs_analyzed": 30,
    "quarantined_count": 2
  }
}
```

### Get Test Summary

```bash
GET /control/e2e/summary/{run_id}?repo_root=/path/to/repo
```

**Response:**
```json
{
  "passed": [...],
  "failed": [...],
  "passed_on_retry": [...],
  "quarantined": [...],
  "skipped": [...],
  "counts": {
    "total": 19,
    "passed": 15,
    "failed": 2,
    "passed_on_retry": 1,
    "quarantined": 1,
    "skipped": 0
  }
}
```

### Get Quarantine List

```bash
GET /control/e2e/quarantine?repo_root=/path/to/repo
```

**Response:**
```json
{
  "quarantine_file": "tests/e2e/quarantine.txt",
  "tests": [
    "tests/e2e/test_slow_network.py::test_timeout_handling",
    "tests/e2e/test_race_condition.py::test_concurrent_updates"
  ],
  "count": 2,
  "exists": true
}
```

### List Recent Runs

```bash
GET /control/e2e/runs?repo_root=/path/to/repo&limit=20
```

### Get Run Details

```bash
GET /control/e2e/run/{run_id}?repo_root=/path/to/repo
```

### Get Run Logs

```bash
GET /control/e2e/logs/{run_id}?repo_root=/path/to/repo&tail=500
```

## Database Schema

Results are stored in `.issue-orchestrator/e2e.db` (SQLite):

```sql
-- Runs table
CREATE TABLE e2e_runs (
  id INTEGER PRIMARY KEY,
  repo_root TEXT NOT NULL,
  orchestrator_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,  -- running/passed/failed/canceled/interrupted/error
  exit_code INTEGER,
  pytest_args TEXT NOT NULL,
  commit_sha TEXT,
  branch TEXT,
  worker_pid INTEGER,
  total_tests INTEGER,
  current_test TEXT,
  duration_seconds REAL,
  note TEXT,
  log_path TEXT
);

-- Per-test results
CREATE TABLE e2e_test_results (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  nodeid TEXT NOT NULL,
  outcome TEXT NOT NULL,      -- passed/failed/skipped/error
  duration_seconds REAL,
  longrepr TEXT,              -- Stack trace for failures
  retry_outcome TEXT,         -- Outcome after retry
  is_quarantined INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES e2e_runs(id)
);
```

## Debugging

### Check if worker is running

```bash
ps aux | grep e2e_worker
```

### View database directly

```bash
sqlite3 .issue-orchestrator/e2e.db "SELECT id, status, total_tests, started_at FROM e2e_runs ORDER BY id DESC LIMIT 5"
```

### View failed tests from last run

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT nodeid, outcome, retry_outcome, longrepr
  FROM e2e_test_results
  WHERE run_id = (SELECT MAX(id) FROM e2e_runs)
    AND outcome = 'failed'
"
```

### Check progress mid-run

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT
    (SELECT COUNT(*) FROM e2e_test_results WHERE run_id = r.id) as completed,
    r.total_tests,
    r.current_test
  FROM e2e_runs r
  WHERE r.status = 'running'
"
```

### View logs

```bash
# Find latest log
ls -lt .issue-orchestrator/logs/e2e/ | head -5

# Tail log
tail -f .issue-orchestrator/logs/e2e/run_*.log
```

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| E2E not auto-triggering | `auto_run_interval_minutes: 0` | Set to positive value |
| Worker exits immediately | Invalid pytest args | Check `pytest_args` path exists |
| "AlreadyRunning" error | Previous worker still running | Stop via API or kill process |
| No progress shown | Old schema | Delete e2e.db, will recreate |
| Tests not resuming | Monolithic test structure | Split into discrete test functions |
| Quarantine not working | Wrong file path | Check `quarantine_file` config |

## Auto-Trigger Logic

E2E auto-triggers when ALL conditions are met:
1. `e2e.enabled: true`
2. `auto_run_interval_minutes > 0`
3. Enough time passed since last E2E run
4. Main branch HEAD has changed since last tested commit
5. No E2E currently running

This ensures E2E only runs when there's new code to test, avoiding redundant test runs.
