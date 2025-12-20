#!/bin/bash
# Orchestrator pre-push hook - validates agent completion
# Installed by issue-orchestrator alongside the wrapper
#
# This hook:
# 1. Validates Agent-Status trailer in latest commit (if agent work)
# 2. Blocks test-skipping patterns (@Disabled, @Ignore, etc.)
#
# Exit codes:
#   0 = ALLOW the push
#   1 = BLOCK the push

set -euo pipefail

# Get the latest commit message
COMMIT_MSG=$(git log -1 --format=%B HEAD 2>/dev/null || echo "")

# Check for Agent-Status trailer (if this looks like agent work)
# Agent work is identified by Co-Authored-By: containing common AI patterns
if echo "$COMMIT_MSG" | grep -qiE "Co-Authored-By:.*(@anthropic|claude|openai|copilot|cursor)"; then
    # This appears to be agent work - check for Agent-Status
    if ! echo "$COMMIT_MSG" | grep -qE "^Agent-Status:"; then
        echo "ERROR: Agent work detected but no Agent-Status trailer found." >&2
        echo "Use 'agent-done' to properly complete agent work." >&2
        exit 1
    fi
fi

# Block test-skipping patterns in staged changes
# These indicate tests were disabled rather than fixed
SKIP_PATTERNS=(
    "@Disabled"
    "@Ignore"
    "assumeTrue.*false"
    "pytest.mark.skip"
    "@pytest.mark.skipif"
    "unittest.skip"
    "xit\\("
    "xdescribe\\("
    "test\\.skip"
    "it\\.skip"
    "describe\\.skip"
)

# Get the diff being pushed
DIFF=$(git diff --cached HEAD~1..HEAD 2>/dev/null || git diff HEAD 2>/dev/null || echo "")

for pattern in "${SKIP_PATTERNS[@]}"; do
    if echo "$DIFF" | grep -qE "^\+.*$pattern"; then
        echo "ERROR: Test-skipping pattern detected: $pattern" >&2
        echo "Fix the test instead of skipping it." >&2
        exit 1
    fi
done

exit 0
