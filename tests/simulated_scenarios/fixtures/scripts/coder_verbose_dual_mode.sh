#!/usr/bin/env bash
set -euo pipefail

# Verbose coder dual-mode script with ANSI codes, spinners, and marker strings.
# Works in both via-local-loop (review exchange) and via-draft-pr (standalone) modes.

echo -e "\x1b[32m[coder] Starting implementation\x1b[0m"
printf "·\r"
echo "Implementing feature: user-authentication-module"
echo -e "\x1b[33mWARNING: deprecated API detected\x1b[0m"
echo "PASS"
echo "Tests passed: 5/5"

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
  --implementation "Implemented user authentication module" \
  --problems "None"
