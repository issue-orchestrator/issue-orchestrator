#!/usr/bin/env bash
set -euo pipefail

# In review-loop mode, output invalid JSON
prompt_path="${1:-}"
completion_path="${ISSUE_ORCHESTRATOR_COMPLETION_PATH:-}"
if [[ -n "$prompt_path" && -f "$prompt_path" ]]; then
  echo "NOT_JSON"
  exit 0
fi
if [[ "$completion_path" == *"completion-coder.json" ]]; then
  echo "NOT_JSON"
  exit 0
fi

# Standalone mode: write normal completion
source "$(dirname "$0")/_write_completion.sh"
write_completion completed "Simulated scenario completed" "None"
