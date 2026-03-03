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

source "$(dirname "$0")/_write_completion.sh"

write_review_loop_completion() {
  write_completion completed "Implemented user authentication module" "None"
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

write_completion completed "Implemented user authentication module" "None"
