#!/bin/bash
# Orchestrator pre-push hook - validates agent completion
# Installed by issue-orchestrator alongside the wrapper
#
# This hook:
# 1. Runs publish gate validation (with cache lookup)
# 2. Validates Agent-Status trailer in latest commit (if agent work)
# 3. Blocks test-skipping patterns (@Disabled, @Ignore, etc.)
#
# Exit codes:
#   0 = ALLOW the push
#   1 = BLOCK the push

set -euo pipefail

# Run publish gate validation if configured
# Uses cache to avoid redundant validation runs
if command -v python3 &> /dev/null; then
    # Prefer module invocation to avoid stale console script entry points.
    if python3 -m issue_orchestrator.entrypoints.cli_tools.prepush_check -v 2>/dev/null; then
        : # Validation passed or not configured
    elif [ $? -eq 1 ]; then
        echo "ERROR: Publish gate validation failed." >&2
        exit 1
    fi
    # Exit code 2 means error/not available - continue with other checks
elif command -v prepush-check &> /dev/null; then
    echo "Running publish gate validation..."
    if ! prepush-check -v; then
        echo "ERROR: Publish gate validation failed." >&2
        echo "Fix the issues and try again." >&2
        exit 1
    fi
fi

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
