#!/bin/bash
# Pre-push wrapper hook - chains project and orchestrator hooks
# Installed by issue-orchestrator to ensure both hooks run
#
# This wrapper:
# 1. Runs the project's original pre-push hook (if any)
# 2. Runs the orchestrator's pre-push hook (trailer validation)
# 3. Writes audit trail to prove hooks executed
# 4. Captures output to .issue-orchestrator/diagnostics/prepush-output.log
#
# Exit codes are passed through - any failure blocks the push

set -euo pipefail

HOOK_DIR="$(dirname "$0")"
LOG_FILE="$HOOK_DIR/pre-push.log"

# Find worktree root for output capture
WORKTREE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -n "$WORKTREE_ROOT" ]; then
    OUTPUT_DIR="$WORKTREE_ROOT/.issue-orchestrator/diagnostics"
    OUTPUT_LOG="$OUTPUT_DIR/prepush-output.log"
    mkdir -p "$OUTPUT_DIR"
else
    OUTPUT_LOG=""
fi

# Write audit trail
log() {
    echo "$(date -Iseconds) $1" >> "$LOG_FILE"
}

log "wrapper-started"

# Run project hook first (their tests/linters)
# Capture output to file while still displaying to terminal
PROJECT_HOOK="$HOOK_DIR/pre-push.project"
PROJECT_EXIT=0
if [ -x "$PROJECT_HOOK" ]; then
    log "project-hook-starting"
    if [ -n "$OUTPUT_LOG" ]; then
        # Capture to file while showing output (use subshell to capture exit code with pipefail)
        set +e
        "$PROJECT_HOOK" "$@" 2>&1 | tee "$OUTPUT_LOG"
        PROJECT_EXIT=${PIPESTATUS[0]}
        set -e
    else
        if "$PROJECT_HOOK" "$@"; then
            PROJECT_EXIT=0
        else
            PROJECT_EXIT=$?
        fi
    fi
    log "project-hook exit=$PROJECT_EXIT"
else
    log "project-hook-skipped (not found or not executable)"
fi

# Run orchestrator hook (trailer validation, pattern blocking)
# Append to the same output file
ORCH_HOOK="$HOOK_DIR/pre-push.orchestrator"
ORCH_EXIT=0
if [ -x "$ORCH_HOOK" ]; then
    log "orchestrator-hook-starting"
    if [ -n "$OUTPUT_LOG" ]; then
        set +e
        "$ORCH_HOOK" "$@" 2>&1 | tee -a "$OUTPUT_LOG"
        ORCH_EXIT=${PIPESTATUS[0]}
        set -e
    else
        if "$ORCH_HOOK" "$@"; then
            ORCH_EXIT=0
        else
            ORCH_EXIT=$?
        fi
    fi
    log "orchestrator-hook exit=$ORCH_EXIT"
else
    log "orchestrator-hook-skipped (not found or not executable)"
fi

# Exit with first failure, printing output location
if [ $PROJECT_EXIT -ne 0 ]; then
    log "wrapper-failed project=$PROJECT_EXIT"
    echo ""
    echo "========================================"
    echo "Pre-push validation FAILED (exit code $PROJECT_EXIT)"
    echo "========================================"
    if [ -n "$OUTPUT_LOG" ]; then
        echo ""
        echo "Full output saved to:"
        echo "  $OUTPUT_LOG"
        echo ""
        echo "To view: cat $OUTPUT_LOG"
    fi
    echo "========================================"
    exit $PROJECT_EXIT
fi

if [ $ORCH_EXIT -ne 0 ]; then
    log "wrapper-failed orchestrator=$ORCH_EXIT"
    echo ""
    echo "========================================"
    echo "Pre-push validation FAILED (exit code $ORCH_EXIT)"
    echo "========================================"
    if [ -n "$OUTPUT_LOG" ]; then
        echo ""
        echo "Full output saved to:"
        echo "  $OUTPUT_LOG"
        echo ""
        echo "To view: cat $OUTPUT_LOG"
    fi
    echo "========================================"
    exit $ORCH_EXIT
fi

log "wrapper-completed"
exit 0
