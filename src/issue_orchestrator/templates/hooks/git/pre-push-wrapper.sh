#!/bin/bash
# Pre-push wrapper hook - chains project and orchestrator hooks
# Installed by issue-orchestrator to ensure both hooks run
#
# This wrapper:
# 1. Runs the project's original pre-push hook (if any)
# 2. Runs the orchestrator's pre-push hook (trailer validation)
# 3. Writes audit trail to prove hooks executed
#
# Output capture is handled by the Python validate_runner if the project
# hook calls 'make validate'. See validate_runner.py for details.
#
# Exit codes are passed through - any failure blocks the push

set -euo pipefail

HOOK_DIR="$(dirname "$0")"
LOG_FILE="$HOOK_DIR/pre-push.log"

# Write audit trail
log() {
    echo "$(date -Iseconds) $1" >> "$LOG_FILE"
}

log "wrapper-started"

# Run project hook first (their tests/linters)
PROJECT_HOOK="$HOOK_DIR/pre-push.project"
PROJECT_EXIT=0
if [ -x "$PROJECT_HOOK" ]; then
    log "project-hook-starting"
    if "$PROJECT_HOOK" "$@"; then
        PROJECT_EXIT=0
    else
        PROJECT_EXIT=$?
    fi
    log "project-hook exit=$PROJECT_EXIT"
else
    log "project-hook-skipped (not found or not executable)"
fi

# Run orchestrator hook (trailer validation, pattern blocking)
ORCH_HOOK="$HOOK_DIR/pre-push.orchestrator"
ORCH_EXIT=0
if [ -x "$ORCH_HOOK" ]; then
    log "orchestrator-hook-starting"
    if "$ORCH_HOOK" "$@"; then
        ORCH_EXIT=0
    else
        ORCH_EXIT=$?
    fi
    log "orchestrator-hook exit=$ORCH_EXIT"
else
    log "orchestrator-hook-skipped (not found or not executable)"
fi

# Exit with first failure
if [ $PROJECT_EXIT -ne 0 ]; then
    log "wrapper-failed project=$PROJECT_EXIT"
    exit $PROJECT_EXIT
fi

if [ $ORCH_EXIT -ne 0 ]; then
    log "wrapper-failed orchestrator=$ORCH_EXIT"
    exit $ORCH_EXIT
fi

log "wrapper-completed"
exit 0
