# Infra

**Purpose**: Configuration loading, diagnostics, process supervision, E2E test runner.

**Boundaries**:
- No direct subprocess usage - use `CommandRunner` port instead
- Exceptions: `supervisor.py`, `ai_diagnose.py`, and `e2e_runner.py` (manage external processes)
- For command execution, inject `CommandRunner` and call `runner.run()`

## E2E Test Runner

The async E2E runner executes pytest in a subprocess with results persisted to SQLite:

| File | Purpose |
|------|---------|
| `e2e_db.py` | SQLite layer for E2E runs and test results |
| `e2e_runner.py` | Worker subprocess manager + auto-trigger logic |

**Key classes**:
- `E2EDB` - Database operations (start_run, finish_run, upsert_test_result, signal_score)
- `E2ERunnerManager` - Subprocess lifecycle (start, stop, status)
- `maybe_trigger_e2e()` - Auto-trigger after agent sessions complete

**Database**: `.issue-orchestrator/e2e.db` with tables `e2e_runs` and `e2e_test_results`

**Example**:
```python
# WRONG - direct subprocess in infra
import subprocess
result = subprocess.run(["git", "status"], capture_output=True)

# RIGHT - use CommandRunner port
from ..ports.command_runner import CommandRunner

def my_check(runner: CommandRunner) -> Check:
    result = runner.run(["git", "status"])
    return Check(name="Git", status="ok" if result.returncode == 0 else "error", ...)
```

**Why**: Keeps infra layer testable and decoupled from execution details. The `CommandRunner` is injected by callers (CLI, web) who provide the real `LocalCommandRunner` implementation.
