# Issue-Orchestrator SQLite Map

## Databases and paths

- Publish job store: `.issue-orchestrator/state/publish_jobs.db`
  - Code: `src/issue_orchestrator/control/job_store.py`
  - Path helper: `get_default_db_path()` in `src/issue_orchestrator/control/job_store.py`

- E2E results: `.issue-orchestrator/e2e.db`
  - Code: `src/issue_orchestrator/infra/e2e_db.py`
  - Used by: `src/issue_orchestrator/infra/e2e_runner.py`, `src/issue_orchestrator/entrypoints/e2e_worker.py`, `src/issue_orchestrator/entrypoints/web.py`, `src/issue_orchestrator/entrypoints/control_api.py`

- Session registry: `.issue-orchestrator/state/session_registry.sqlite`
  - Code: `src/issue_orchestrator/execution/terminal_subprocess.py`

## Registry and maintenance

- Registry: `src/issue_orchestrator/infra/sqlite_registry.py`
  - Data-driven list of SQLite databases (name, path, enabled, backup/pragmas).
- Maintenance/backup utilities: `src/issue_orchestrator/infra/sqlite_maintenance.py`
  - Applies WAL + FULL pragmas on startup
  - Runs `PRAGMA quick_check` for doctor
  - Performs backups/retention based on `sqlite_backup` config
  - Backups stored at `.issue-orchestrator/backups/sqlite/<db_key>/daily/` and `weekly/`

## Recovery (manual)

1. Stop the orchestrator.
2. Pick the newest backup for the DB key.
3. Replace the DB file with the backup.
4. Restart the orchestrator and re-run doctor.

## Connection setup and settings

- JobStore connection setup (`JobStore._get_connection` in `src/issue_orchestrator/control/job_store.py`)
  - Uses `sqlite3.connect(..., check_same_thread=False, isolation_level=None)`
  - Applies pragmas via `apply_pragmas` (WAL + FULL + foreign keys)

- E2EDB connection setup (`E2EDB._connect` in `src/issue_orchestrator/infra/e2e_db.py`)
  - Uses `sqlite3.connect(..., timeout=10.0)`
  - Applies pragmas via `apply_pragmas` (WAL + FULL + foreign keys)

- Subprocess registry connection setup (`_SubprocessRegistry._connect` in `src/issue_orchestrator/execution/terminal_subprocess.py`)
  - Uses default `sqlite3.connect(self._db_path)`
  - Applies pragmas via `apply_pragmas` (WAL + FULL + foreign keys)

## Existing corruption handling

- Subprocess registry handles SQLite corruption:
  - On `sqlite3.DatabaseError`, moves DB to `*.sqlite.corrupt` and recreates schema.
  - Code: `_SubprocessRegistry._handle_corrupt_db` in `src/issue_orchestrator/execution/terminal_subprocess.py`

- E2EDB and JobStore do not have built-in corruption recovery beyond exceptions.

## Usage notes

- E2E runner stores results for long-running tests; DB is read by web/control API for dashboard.
- JobStore is optional persistence for publish jobs; enabled via `build_orchestrator` wiring in `src/issue_orchestrator/entrypoints/bootstrap.py`.
