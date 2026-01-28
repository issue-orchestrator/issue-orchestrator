#!/bin/bash
# Block git commit/push --no-verify for GitHub Copilot CLI agents
# This hook is called by Copilot CLI preToolUse mechanism
#
# Output: JSON to stdout
#   {"permissionDecision": "allow"} = ALLOW the command
#   {"permissionDecision": "deny", "permissionDecisionReason": "..."} = BLOCK the command
#
# Configured in .github/hooks/hooks.json:
# {
#   "version": 1,
#   "hooks": {
#     "preToolUse": [{
#       "type": "command",
#       "bash": ".github/hooks/block-no-verify.sh"
#     }]
#   }
# }

set -euo pipefail

# Read input from stdin (JSON with toolName and toolArgs)
input=$(cat)

# Resolve python3 (required for JSON parsing)
python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: python3 is required for orchestrator hooks."}'
    exit 0
fi

# Extract the command being executed
# Copilot sends: {"toolName": "bash", "toolArgs": "{\"command\": \"git push ...\"}"}
hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
parse_script="$hook_dir/parse_hook_input.py"
if [[ ! -f "$parse_script" ]]; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: missing parse_hook_input.py. Reinstall hooks."}'
    exit 0
fi
command=$("$python_bin" "$parse_script" <<< "$input" 2>&1) || {
    echo "{\"permissionDecision\": \"deny\", \"permissionDecisionReason\": \"BLOCKED: failed to parse hook input. Error: $command\"}"
    exit 0
}

# Fail closed: if input was non-empty but command is empty, parsing failed silently
if [[ -z "$command" && -n "$input" ]]; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: unable to extract command from hook input. Input may be malformed."}'
    exit 0
fi

# Allow a dry-run no-verify push for reuse preflight when enabled.
allow_flag=""
search_dir="$PWD"
while [[ -n "$search_dir" && "$search_dir" != "/" ]]; do
    candidate="$search_dir/.issue-orchestrator/allow-no-verify-dry-run"
    if [[ -f "$candidate" ]]; then
        allow_flag="$candidate"
        break
    fi
    search_dir="$(dirname "$search_dir")"
done
if echo "$command" | grep -qE "git\\s+push" \
    && echo "$command" | grep -qE -- "--dry-run" \
    && echo "$command" | grep -qE -- "--no-verify" \
    && [[ -n "$allow_flag" ]] && [[ -f "$allow_flag" ]]; then
    echo '{"permissionDecision": "allow"}'
    exit 0
fi

# Check for --no-verify bypass attempts
if echo "$command" | grep -qE "git\s+(commit|push).*--no-verify"; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: --no-verify is forbidden. Pre-push hooks must run."}'
    exit 0
fi

# Also catch the flag before the subcommand
if echo "$command" | grep -qE "git\s+--no-verify"; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: --no-verify is forbidden."}'
    exit 0
fi

# Catch -n shorthand for --no-verify in commit
if echo "$command" | grep -qE "git\s+commit.*\s-n\s"; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: -n (--no-verify) is forbidden."}'
    exit 0
fi

# Catch attempts to disable hooks via config
if echo "$command" | grep -qE "git\s+-c\s+core\.hooksPath=/dev/null"; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: Disabling hooks via core.hooksPath is forbidden."}'
    exit 0
fi

# Block gh pr merge - agents cannot merge PRs
if echo "$command" | grep -qE "gh\s+pr\s+merge"; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: Agents cannot merge PRs. Only humans can merge."}'
    exit 0
fi

# Block gh api calls to merge endpoint
if echo "$command" | grep -qE "gh\s+api\s+.*pulls/[0-9]+/merge"; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: Agents cannot merge PRs via API. Only humans can merge."}'
    exit 0
fi

# Allow the command
echo '{"permissionDecision": "allow"}'
exit 0
