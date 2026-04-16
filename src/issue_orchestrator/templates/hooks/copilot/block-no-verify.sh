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

input="$(< /dev/stdin)"

# Fail closed if the Python evaluator is unavailable or errors. This hook is an
# enforcement layer; allowing execution through a broken hook would bypass the
# repo guardrails entirely.

python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: python3 is required for orchestrator hooks. Fix PATH or install python3."}'
    exit 0
fi

hook_pythonpath="${ORCHESTRATOR_HOOK_PYTHONPATH:-}"
repo_local_hook=""
if [[ -z "$hook_pythonpath" ]]; then
    hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo_root="$(cd "$hook_dir/../.." && pwd)"
    if [[ -f "$repo_root/scripts/agent-hooks/block_no_verify.py" ]]; then
        repo_local_hook="$repo_root/scripts/agent-hooks/block_no_verify.py"
    fi
    if [[ -d "$repo_root/src/issue_orchestrator" ]]; then
        hook_pythonpath="$repo_root/src"
    fi
fi

set +e
if [[ -n "$repo_local_hook" ]]; then
    output=$("$python_bin" "$repo_local_hook" --mode copilot <<< "$input" 2>/dev/null)
elif [[ -n "$hook_pythonpath" ]]; then
    output=$(PYTHONPATH="$hook_pythonpath${PYTHONPATH:+:$PYTHONPATH}" \
        "$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode copilot <<< "$input" 2>/dev/null)
else
    output=$("$python_bin" -m issue_orchestrator.infra.hooks.block_no_verify --mode copilot <<< "$input" 2>/dev/null)
fi
status=$?
set -e
if [[ $status -ne 0 ]]; then
    echo '{"permissionDecision": "deny", "permissionDecisionReason": "BLOCKED: hook evaluation failed."}'
    exit 0
fi

echo "$output"
exit 0
