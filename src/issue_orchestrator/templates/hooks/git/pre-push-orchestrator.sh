#!/bin/bash
# Orchestrator pre-push hook - validates agent code quality
# Installed by issue-orchestrator alongside the wrapper
#
# This hook runs publish gate validation with cache lookup.
#
# Exit codes:
#   0 = ALLOW the push
#   1 = BLOCK the push

set -euo pipefail

# Run publish gate validation if configured
# Uses cache to avoid redundant validation runs
PYTHON_BIN=""
if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
fi

if [ -n "$PYTHON_BIN" ]; then
    # Prefer module invocation to avoid stale console script entry points.
    if "$PYTHON_BIN" -m issue_orchestrator.entrypoints.cli_tools.prepush_check -v; then
        : # Validation passed or not configured
    elif [ $? -eq 1 ]; then
        echo "ERROR: Publish gate validation failed." >&2
        exit 1
    fi
    # Exit code 2 means error/not available - continue with other checks
elif command -v prepush-check >/dev/null 2>&1; then
    echo "Running publish gate validation..."
    if ! prepush-check -v; then
        echo "ERROR: Publish gate validation failed." >&2
        echo "Fix the issues and try again." >&2
        exit 1
    fi
fi

exit 0
