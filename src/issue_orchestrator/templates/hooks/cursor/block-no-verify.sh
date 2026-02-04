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

python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
    echo '{"permission": "deny", "userMessage": "BLOCKED: python3 is required for orchestrator hooks."}'
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

if [[ -n "$hook_pythonpath" ]]; then
    output=$(PYTHONPATH="$hook_pythonpath${PYTHONPATH:+:$PYTHONPATH}" \
        "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode cursor <<< "$input" 2>/dev/null)
else
    output=$("$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode cursor <<< "$input" 2>/dev/null)
fi
status=$?
if [[ $status -ne 0 ]]; then
    echo '{"permission": "deny", "userMessage": "BLOCKED: hook evaluation failed."}'
    exit 0
fi

echo "$output"
exit 0
