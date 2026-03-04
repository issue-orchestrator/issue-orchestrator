#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_write_completion.sh"

write_review_loop_completion() {
  write_completion completed "Simulated scenario completed" "None"
}

prompt_path="${1:-}"
if [[ -n "$prompt_path" && -f "$prompt_path" ]]; then
  write_review_loop_completion
  echo '{"response_type":"ok","response_text":"Applied fixes"}'
  exit 0
fi

if [[ "${ISSUE_ORCHESTRATOR_COMPLETION_PATH:-}" == *"completion-coder.json" ]]; then
  write_review_loop_completion
  echo '{"response_type":"ok","response_text":"Applied fixes"}'
  exit 0
fi

write_completion completed "Simulated scenario completed" "None"
