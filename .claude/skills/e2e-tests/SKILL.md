---
name: e2e-tests
description: Run end-to-end tests against a target repository. Use when running e2e tests, debugging e2e failures, or setting up e2e test infrastructure.
---

# E2E Test Runner

Async E2E test runner that executes tests locally with dashboard visibility and resume support.

## When to Use

- Running or debugging E2E tests
- Configuring E2E auto-trigger
- Investigating E2E failures
- Managing quarantined tests
- Understanding E2E progress/status

## Configuration

E2E settings are defined in `src/issue_orchestrator/infra/settings_schema.py` (`E2ESettings` model) and drive the web settings dialog, API, and wizard defaults. The schema is the single source of truth.

```yaml
# .issue-orchestrator/config/*.yaml
e2e:
  enabled: true
  auto_run_interval_minutes: 30    # 0 = manual only
  pytest_args: ["tests/e2e", "-v"]
  allow_retry_once: true           # Retry failing tests once
  quarantine_file: "tests/e2e/quarantine.txt"
  survive_restart: true            # Let worker continue if orchestrator restarts
```

## Key Files

| File | Purpose |
|------|---------|
| `infra/e2e_db.py` | SQLite persistence for runs and results |
| `infra/e2e_runner.py` | Worker manager, auto-trigger logic |
| `entrypoints/e2e_worker.py` | Pytest subprocess with result plugin |
| `entrypoints/control_api.py` | API endpoints at `/control/e2e/*` |

## Database Schema

Results stored in `.issue-orchestrator/e2e.db`:

```sql
-- Runs table
e2e_runs: id, status, started_at, finished_at, total_tests, current_test, worker_pid, ...

-- Per-test results
e2e_test_results: run_id, nodeid, outcome, duration_seconds, longrepr, retry_outcome, is_quarantined
```

## Progress Tracking

The runner tracks progress in real-time:
- `total_tests`: Set after pytest collection phase
- `current_test`: Updated as each test starts
- `completed/passed/failed/skipped`: Counted from results table

Dashboard polls `/control/e2e/status` every 2 seconds while running.

## Resume Support

When orchestrator restarts mid-run:

1. **Orphan detection**: `start_run()` checks if existing "running" run has dead `worker_pid`
2. **Mark interrupted**: Dead runs marked as `status='interrupted'`
3. **Resume**: `start_or_resume()` finds interrupted runs, gets passed nodeids
4. **Skip passed**: Passes `--deselect <nodeid>` to pytest for each passed test

**Test structure for resumability:**
```python
# Good - each function is a checkpoint
def test_create_issue(): ...
def test_create_pr(): ...
def test_review_cycle(): ...

# Bad - monolithic, no partial progress
def test_entire_workflow(): ...
```

## API Endpoints

```bash
# Start E2E (or resume interrupted)
curl -X POST http://localhost:8080/control/e2e/start \
  -H "Content-Type: application/json" \
  -d '{"repo_root": "'$(pwd)'"}'

# Stop running E2E
curl -X POST http://localhost:8080/control/e2e/stop \
  -H "Content-Type: application/json" \
  -d '{"repo_root": "'$(pwd)'"}'

# Get status with progress
curl "http://localhost:8080/control/e2e/status?repo_root=$(pwd)" | jq

# List recent runs
curl "http://localhost:8080/control/e2e/runs?repo_root=$(pwd)" | jq

# Get run details with test results
curl "http://localhost:8080/control/e2e/run/1?repo_root=$(pwd)" | jq
```

## Debugging E2E Failures

### Check Status
```bash
# Via API
curl -s "http://localhost:8080/control/e2e/status?repo_root=$(pwd)" | jq

# Check if worker is running
ps aux | grep e2e_worker

# View database
sqlite3 .issue-orchestrator/e2e.db "SELECT id, status, total_tests, started_at FROM e2e_runs ORDER BY id DESC LIMIT 5"
```

### View Logs
```bash
# Find latest log
ls -lt .issue-orchestrator/logs/e2e/ | head -5

# Tail log
tail -f .issue-orchestrator/logs/e2e/run_*.log
```

### View Failed Tests
```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT nodeid, outcome, longrepr
  FROM e2e_test_results
  WHERE run_id = (SELECT MAX(id) FROM e2e_runs)
    AND outcome = 'failed'
"
```

### Check Progress Mid-Run
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

## Quarantine Management

Quarantine file lists known flaky tests excluded from failure count:

```
# tests/e2e/quarantine.txt
# Known flaky tests - excluded from required runs
tests/e2e/test_slow_network.py::test_timeout_handling
tests/e2e/test_race_condition.py::test_concurrent_updates
```

Quarantined tests still run but:
- Marked with `is_quarantined=1` in results
- Excluded from `get_failed_tests()`
- Don't affect pass/fail status

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| E2E not auto-triggering | `auto_run_interval_minutes: 0` | Set to positive value |
| Worker exits immediately | Invalid pytest args | Check `pytest_args` path exists |
| "AlreadyRunning" error | Previous worker still running | Stop via API or kill process |
| No progress shown | Old schema without `total_tests` | Delete e2e.db, will recreate |
| Tests not resuming | Monolithic test structure | Split into discrete test functions |

## Auto-Trigger Logic

E2E auto-triggers when ALL conditions met:
1. `e2e.enabled: true`
2. `auto_run_interval_minutes > 0`
3. At least one agent session completed since last check
4. No E2E currently running
5. Enough time passed since last E2E run

Check auto-trigger in logs:
```bash
grep "E2E auto-trigger" .issue-orchestrator/state/logs/orchestrator.log
```
