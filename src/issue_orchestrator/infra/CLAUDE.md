# Infra

**Purpose**: Configuration loading, diagnostics, process supervision.

**Boundaries**:
- No direct subprocess usage - use `CommandRunner` port instead
- Exceptions: `supervisor.py` and `ai_diagnose.py` (manage external processes)
- For command execution, inject `CommandRunner` and call `runner.run()`

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
