"""Foreground launch of an interactive command (inherits the user's terminal).

Distinct from the orchestrated runners in this package: ``LocalCommandRunner``
captures output and ``SubprocessAgentRunner`` runs headless with ``DEVNULL``
stdin. This helper is for human-driven interactive sessions — e.g. launching an
AI agent CLI so the user can converse with it directly — where the child must
fully own stdin/stdout/stderr. It lives in the execution layer because process
spawning is an execution concern, not an entrypoint one.
"""

import subprocess
from pathlib import Path


def run_interactive(command: list[str], cwd: Path) -> int:
    """Run ``command`` in the foreground in ``cwd`` and return its exit code.

    stdin/stdout/stderr are inherited from the parent so the child owns the
    terminal for the duration of the session; control returns to the caller
    when the child exits.
    """
    result = subprocess.run(command, cwd=str(cwd))
    return result.returncode
