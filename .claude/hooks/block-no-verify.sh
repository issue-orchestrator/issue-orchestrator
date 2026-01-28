#!/bin/bash
# Block git commit/push --no-verify for Claude Code agents
# This hook is called by Claude Code PreToolUse mechanism
#
# Exit codes:
#   0 = ALLOW the command to execute
#   2 = BLOCK the command (Claude Code convention)
#
# Configured in .claude/settings.json:
# {
#   "hooks": {
#     "PreToolUse": [{
#       "matcher": "Bash",
#       "hooks": [{"type": "command", "command": ".claude/hooks/block-no-verify.sh"}]
#     }]
#   }
# }

set -euo pipefail

# Read input from stdin (JSON with tool_input)
input=$(cat)

# Resolve python3 (required for JSON parsing and preflight check)
python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
    echo "BLOCKED: python3 is required for orchestrator hooks. Fix PATH or install python3." >&2
    exit 2
fi

# Extract the command being executed
# Claude Code sends: {"tool_input": {"command": "git push ..."}}
hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command=$("$python_bin" "$hook_dir/parse_hook_input.py" <<< "$input" 2>/dev/null || echo "")

# Allow a dry-run no-verify push for reuse preflight when enabled.
allow_script="$hook_dir/allow_git_push.py"
if [[ ! -f "$allow_script" ]]; then
    echo "BLOCKED: missing $allow_script. Reinstall or update the repo hooks." >&2
    exit 2
fi

allow_preflight="false"
if "$python_bin" "$allow_script" "$command"; then
    allow_preflight="true"
fi

if [[ "$allow_preflight" == "true" ]]; then
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
    if [[ -n "$allow_flag" ]] && [[ -f "$allow_flag" ]]; then
        exit 0
    fi
fi

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

# Block gh pr merge - agents should not merge PRs
if echo "$command" | grep -qE "gh\s+pr\s+merge"; then
    echo "BLOCKED: Agents cannot merge PRs. Only humans can merge." >&2
    exit 2
fi

# Block gh api calls to merge endpoint
if echo "$command" | grep -qE "gh\s+api\s+.*pulls/[0-9]+/merge"; then
    echo "BLOCKED: Agents cannot merge PRs via API. Only humans can merge." >&2
    exit 2
fi

# Allow the command
exit 0
