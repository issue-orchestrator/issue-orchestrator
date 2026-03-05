#!/bin/bash
# Script agent that always requests changes - for testing rework cycles

set -ex

# Ensure common tools are in PATH (homebrew, git, gh)
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# Add reviewer-done to PATH (derive from script location)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PATH="${PATH}:${REPO_ROOT}/src/issue_orchestrator/scripts"

# Always request changes (for testing rework cycles)
reviewer-done changes_requested \
  --issues "E2E TEST: Intentionally requesting changes to test rework cycle" \
  --risk low

exit 0
