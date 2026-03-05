#!/bin/bash
# Scripted review agent that approves unless PR title indicates E2E rework.

set -ex

# Ensure common tools are in PATH (homebrew, git, gh)
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# Add reviewer-done to PATH (derive from script location)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PATH="${PATH}:${REPO_ROOT}/src/issue_orchestrator/scripts"

if [[ -z "${PR_NUMBER:-}" ]]; then
  echo "PR_NUMBER not set; defaulting to approve" >&2
  reviewer-done approved \
    --summary "Auto-approve: PR_NUMBER not provided" \
    --risk low
  exit 0
fi

PR_INFO=$(gh pr view "${PR_NUMBER}" --json title,headRefName,body --jq '"\(.title)\n\(.headRefName)\n\(.body)"' 2>/dev/null || echo "")

NEEDS_REWORK=0
if echo "$PR_INFO" | grep -qi "E2E-REWORK" || echo "$PR_INFO" | grep -qi "e2e-rework"; then
  NEEDS_REWORK=1
else
  ISSUE_NUMBER=$(echo "$PR_INFO" | grep -oE '#[0-9]+' | head -n1 | tr -d '#')
  if [[ -z "${ISSUE_NUMBER:-}" ]]; then
    ISSUE_NUMBER=$(echo "$PR_INFO" | grep -oE '^[0-9]+-' | head -n1 | tr -d '-')
  fi
  if [[ -n "${ISSUE_NUMBER:-}" ]]; then
    ISSUE_LABELS=$(gh issue view "${ISSUE_NUMBER}" --json labels --jq '.labels[].name' 2>/dev/null || echo "")
    if echo "$ISSUE_LABELS" | grep -qi "io:e2e:rework_cycles"; then
      NEEDS_REWORK=1
    fi
  fi
fi

if [[ "$NEEDS_REWORK" -eq 1 ]]; then
  reviewer-done changes_requested \
    --issues "E2E rework flow: requesting changes for escalation test" \
    --risk low
else
  reviewer-done approved \
    --summary "Auto-approve: review-decider passed" \
    --risk low
fi

exit 0
