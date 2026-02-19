#!/usr/bin/env bash
set -euo pipefail

write_review_loop_completion() {
  completion_path="${ISSUE_ORCHESTRATOR_COMPLETION_PATH:-}"
  if [[ -z "$completion_path" ]]; then
    return
  fi
  if [[ "$completion_path" = /* ]]; then
    resolved_path="$completion_path"
  else
    resolved_path="$(pwd)/$completion_path"
  fi
  mkdir -p "$(dirname "$resolved_path")"
  cat > "$resolved_path" <<'JSON'
{"session_id":"sim-review-loop","outcome":"completed"}
JSON
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

python -m issue_orchestrator.entrypoints.cli_tools.agent_done completed \
  --implementation "Simulated scenario completed" \
  --problems "None"
