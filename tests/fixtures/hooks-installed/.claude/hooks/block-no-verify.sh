#!/bin/bash
# Block git commit/push --no-verify for Claude Code agents
# This hook is called by Claude Code PreToolUse mechanism
#
# Exit codes:
#   0 = ALLOW the command to execute
#   2 = BLOCK the command (Claude Code convention)

set -euo pipefail

# Read input from stdin (JSON with tool_input)
input=$(cat)

# Extract the command being executed
# Claude Code sends: {"tool_input": {"command": "git push ..."}}
command=$(echo "$input" | jq -r '.tool_input.command // ""' 2>/dev/null || echo "")

# Check for --no-verify bypass attempts
if echo "$command" | grep -qE "git\s+(commit|push).*--no-verify"; then
    echo "BLOCKED: --no-verify is forbidden. Pre-push hooks must run." >&2
    exit 2
fi

# Also catch the flag before the subcommand
if echo "$command" | grep -qE "git\s+--no-verify"; then
    echo "BLOCKED: --no-verify is forbidden." >&2
    exit 2
fi

# Catch -n shorthand for --no-verify in commit
if echo "$command" | grep -qE "git\s+commit.*\s-n\s"; then
    echo "BLOCKED: -n (--no-verify) is forbidden." >&2
    exit 2
fi

# Catch attempts to disable hooks via config
if echo "$command" | grep -qE "git\s+-c\s+core\.hooksPath=/dev/null"; then
    echo "BLOCKED: Disabling hooks via core.hooksPath is forbidden." >&2
    exit 2
fi

# Allow the command
exit 0
