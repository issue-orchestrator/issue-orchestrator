#!/usr/bin/env bash
set -euo pipefail

prompt_path="${1:-}"
completion_path="${ISSUE_ORCHESTRATOR_COMPLETION_PATH:-}"
if [[ -n "$prompt_path" && -f "$prompt_path" ]]; then
  echo "coder failed" >&2
  exit 1
fi
if [[ "$completion_path" == *"completion-coder.json" ]]; then
  echo "coder failed" >&2
  exit 1
fi

python -m issue_orchestrator.entrypoints.cli_tools.agent_done completed \
  --implementation "Simulated scenario completed" \
  --problems "None"
