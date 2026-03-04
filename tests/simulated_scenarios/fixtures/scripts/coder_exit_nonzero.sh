#!/usr/bin/env bash
set -euo pipefail

# In review-loop mode, fail with nonzero exit
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

# Standalone mode: write normal completion
source "$(dirname "$0")/_write_completion.sh"
write_completion completed "Simulated scenario completed" "None"
