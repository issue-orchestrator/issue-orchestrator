#!/usr/bin/env bash
set -euo pipefail

prompt_path="${1:-}"
round_num=""
if [[ -n "$prompt_path" && -f "$prompt_path" ]]; then
  round_num=$(grep -Eo 'Round [0-9]+' "$prompt_path" | head -n1 | awk '{print $2}')
fi

if [[ -z "$round_num" ]]; then
  round_num=1
fi

if [[ "$round_num" -lt 2 ]]; then
  echo '{"response_type":"disagree","getting_closer":true,"response_text":"Disagree with changes"}' \
    > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
else
  echo '{"response_type":"ok","getting_closer":true,"response_text":"Resolved"}' \
    > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"
fi
