#!/bin/bash
# Block git commit/push --no-verify for Cursor agents
# This hook is called by Cursor beforeShellExecution mechanism
#
# Output: JSON to stdout
#   {"permission": "allow"} = ALLOW the command
#   {"permission": "deny", "userMessage": "..."} = BLOCK the command
#
# Configured in .cursor/hooks.json:
# {
#   "beforeShellExecution": [{
#     "command": ".cursor/hooks/block-no-verify.sh",
#     "output": "json"
#   }]
# }

set -euo pipefail

# Read input from stdin (JSON with command)
input=$(cat)

# Extract the command being executed
# Cursor may send: {"command": "git push ..."} or {"tool_input": {"command": "..."}}
command=$(echo "$input" | jq -r '.tool_input.command // .command // ""' 2>/dev/null || echo "")

# Check for --no-verify bypass attempts
if echo "$command" | grep -qE "git\s+(commit|push).*--no-verify"; then
    echo '{"permission": "deny", "userMessage": "BLOCKED: --no-verify is forbidden. Pre-push hooks must run."}'
    exit 0
fi

# Also catch the flag before the subcommand
if echo "$command" | grep -qE "git\s+--no-verify"; then
    echo '{"permission": "deny", "userMessage": "BLOCKED: --no-verify is forbidden."}'
    exit 0
fi

# Catch -n shorthand for --no-verify in commit
if echo "$command" | grep -qE "git\s+commit.*\s-n\s"; then
    echo '{"permission": "deny", "userMessage": "BLOCKED: -n (--no-verify) is forbidden."}'
    exit 0
fi

# Catch attempts to disable hooks via config
if echo "$command" | grep -qE "git\s+-c\s+core\.hooksPath=/dev/null"; then
    echo '{"permission": "deny", "userMessage": "BLOCKED: Disabling hooks via core.hooksPath is forbidden."}'
    exit 0
fi

# Allow the command
echo '{"permission": "allow"}'
exit 0
