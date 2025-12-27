#!/bin/bash
# Script agent that completes immediately - for e2e testing orchestrator lifecycle

set -ex  # -x for debugging

# Ensure common tools are in PATH (homebrew, git, gh)
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# Add agent-done to PATH (derive from script location)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PATH="${PATH}:${REPO_ROOT}/src/issue_orchestrator/scripts"

# Get issue number from branch name (format: NNN-title-slug)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
ISSUE_NUMBER=$(echo "$BRANCH" | grep -oE '^[0-9]+')

echo "E2E test completed at $(date)" > e2e-test-output.txt
git add e2e-test-output.txt
git commit -m "E2E test: verify orchestrator lifecycle"

# Signal completion - orchestrator will push branch and create PR
# If E2E_PR_LABELS is set (from config), pass labels for PR tagging
if [[ -n "$E2E_PR_LABELS" ]]; then
    # Convert comma-separated to space-separated for --pr-labels
    LABELS=$(echo "$E2E_PR_LABELS" | tr ',' ' ')
    agent-done completed \
        --implementation "E2E test completed successfully" \
        --problems "None" \
        --pr-labels $LABELS
else
    agent-done completed \
        --implementation "E2E test completed successfully" \
        --problems "None"
fi

exit 0
