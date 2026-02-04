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

input="$(< /dev/stdin)"

fallback_block_no_verify() {
    local payload="$1"
    [[ "$payload" == *"--no-verify"* && "$payload" == *"git"* ]]
}

python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
    if fallback_block_no_verify "$input"; then
        echo '{"permission": "deny", "userMessage": "BLOCKED: python3 missing; blocked --no-verify."}'
    else
        echo '{"permission": "allow"}'
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
    output=$(PYTHONPATH="$hook_pythonpath${PYTHONPATH:+:$PYTHONPATH}" \
        "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode cursor <<< "$input" 2>/dev/null)
else
    output=$("$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode cursor <<< "$input" 2>/dev/null)
fi
status=$?
set -e
if [[ $status -ne 0 ]]; then
    if fallback_block_no_verify "$input"; then
        echo '{"permission": "deny", "userMessage": "BLOCKED: hook evaluation failed; blocked --no-verify."}'
    else
        echo '{"permission": "allow"}'
    fi
    exit 0
fi

echo "$output"
exit 0
