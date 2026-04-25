"""Shared Python interpreter resolution for generated hook scripts."""

from __future__ import annotations

import os
import shlex
import sys

ORCHESTRATOR_PYTHON_ENV = "ISSUE_ORCHESTRATOR_PYTHON"


def resolve_issue_orchestrator_python() -> str:
    """Return the interpreter path to bake into generated hook scripts."""
    override = os.environ.get(ORCHESTRATOR_PYTHON_ENV)
    if override and os.access(override, os.X_OK) and os.path.isfile(override):
        return override
    return sys.executable


def shell_quote_issue_orchestrator_python() -> str:
    """Return the baked interpreter path as a shell-safe literal."""
    return shlex.quote(resolve_issue_orchestrator_python())
