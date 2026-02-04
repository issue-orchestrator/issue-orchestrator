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

input="$(< /dev/stdin)"

fallback_block_no_verify() {
    if echo "$1" | grep -q -- "--no-verify" && echo "$1" | grep -q -- "git"; then
        return 0
    fi
    return 1
}

python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
    if fallback_block_no_verify "$input"; then
        echo "BLOCKED: python3 missing; blocked --no-verify." >&2
        exit 2
    fi
    exit 0
fi

hook_pythonpath="${ORCHESTRATOR_HOOK_PYTHONPATH:-}"
if [[ -z "$hook_pythonpath" ]]; then
    hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo_root="$(cd "$hook_dir/../.." && pwd)"
    if [[ -d "$repo_root/src/issue_orchestrator" ]]; then
        hook_pythonpath="$repo_root/src"
    fi
fi

set +e
if [[ -n "$hook_pythonpath" ]]; then
    PYTHONPATH="$hook_pythonpath${PYTHONPATH:+:$PYTHONPATH}" \
        "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode claude <<< "$input"
else
    "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode claude <<< "$input"
fi
status=$?
set -e
if [[ $status -eq 0 || $status -eq 2 ]]; then
    exit $status
fi

if fallback_block_no_verify "$input"; then
    echo "BLOCKED: hook evaluation failed; blocked --no-verify." >&2
    exit 2
fi
exit 0
