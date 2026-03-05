#!/bin/bash
# Script agent that completes immediately - for e2e testing orchestrator lifecycle

set -ex  # -x for debugging

log() {
    # Timestamped logs for session.log visibility
    echo "[$(date +'%Y-%m-%d %H:%M:%S%z')] $*"
}

# Ensure common tools are in PATH (homebrew, git, gh)
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# Add coding-done to PATH (derive from script location)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PATH="${PATH}:${REPO_ROOT}/src/issue_orchestrator/scripts"

# Get issue number from branch name (format: NNN-title-slug)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
ISSUE_NUMBER=$(echo "$BRANCH" | grep -oE '^[0-9]+')

log "Starting e2e completion script"
log "Branch: $(git rev-parse --abbrev-ref HEAD)"
log "Writing output file"
echo "E2E test completed at $(date)" > e2e-test-output.txt
git add e2e-test-output.txt
log "Running git commit (hooks enabled)"
time git commit \
    -m "E2E test: verify orchestrator lifecycle" \
    --trailer "Agent-Status: completed" \
    --trailer "Agent-Implementation: E2E test completed successfully" \
    --trailer "Agent-Problems: None"
log "git commit finished with status $?"

# Signal completion - orchestrator will push branch and create PR
# If E2E_PR_LABELS is set (from config), pass labels for PR tagging
LABELS=""
if [[ -n "$E2E_PR_LABELS" ]]; then
    # Convert comma-separated to space-separated for --pr-labels
    LABELS=$(echo "$E2E_PR_LABELS" | tr ',' ' ')
fi
if [[ -n "$ORCHESTRATOR_AGENT_LABEL" ]]; then
    LABELS="${LABELS} ${ORCHESTRATOR_AGENT_LABEL}"
fi

log "Running coding-done"
if [[ -n "$LABELS" ]]; then
    time coding-done completed \
        --implementation "E2E test completed successfully" \
        --problems "None" \
        --pr-labels $LABELS
else
    time coding-done completed \
        --implementation "E2E test completed successfully" \
        --problems "None"
fi
log "coding-done finished with status $?"

exit 0
