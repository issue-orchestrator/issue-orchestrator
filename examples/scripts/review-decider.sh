#!/bin/bash
# Scripted review agent that approves unless PR title indicates E2E rework.

set -ex

# Ensure common tools are in PATH (homebrew, git, gh)
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# Add agent-done to PATH (derive from script location)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PATH="${PATH}:${REPO_ROOT}/src/issue_orchestrator/scripts"

if [[ -z "${PR_NUMBER:-}" ]]; then
  echo "PR_NUMBER not set; defaulting to approve" >&2
  agent-done approved \
    --summary "Auto-approve: PR_NUMBER not provided" \
    --risk low
  exit 0
fi

PR_TITLE=$(gh pr view "${PR_NUMBER}" --json title --jq .title 2>/dev/null || echo "")

if echo "$PR_TITLE" | grep -q "E2E-REWORK"; then
  agent-done changes_requested \
    --issues "E2E rework flow: requesting changes for escalation test" \
    --risk low
else
  agent-done approved \
    --summary "Auto-approve: review-decider passed" \
    --risk low
fi

exit 0
