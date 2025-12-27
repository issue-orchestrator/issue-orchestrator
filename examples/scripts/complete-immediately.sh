#!/bin/bash
# Script agent that completes immediately - for e2e testing orchestrator lifecycle

set -ex  # -x for debugging

# Ensure common tools are in PATH (homebrew, git, gh)
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# E2E filter label - passed from orchestrator config, used to tag PRs for cleanup
E2E_FILTER_LABEL="${ORCHESTRATOR_FILTER_LABEL}"

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
# Pass --pr-labels if filter label is configured (for e2e cleanup)
if [[ -n "$ORCHESTRATOR_FILTER_LABEL" ]]; then
    agent-done completed \
        --implementation "E2E test completed successfully" \
        --problems "None" \
        --pr-labels "$ORCHESTRATOR_FILTER_LABEL"
else
    agent-done completed \
        --implementation "E2E test completed successfully" \
        --problems "None"
fi

exit 0
